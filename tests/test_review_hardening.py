#!/usr/bin/env python3
"""/api/review 加固：automation.lock、版本校验 409、只提交本次写入文件。

全部在临时 git 仓库与临时 MEMORY_HUB 目录中运行，不触碰 ~/.memory-hub 与 ~/llm-wiki。
"""
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
_ENV_KEYS = ("WIKI_PATH", "MEMORY_HUB_STAGING", "MEMORY_HUB_DATA", "MEMORY_HUB_EXPERIENCE_DB")
PAGE = "---\ntitle: 'p'\ntype: note\nstatus: candidate\n---\n\nbody\n"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


class ReviewHardeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="review-hardening-"))
        cls.saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        cls.wiki = cls.tmp / "wiki"
        cls.data = cls.tmp / "data"
        (cls.wiki / "notes").mkdir(parents=True)
        (cls.tmp / "staging" / "pages").mkdir(parents=True)
        cls.data.mkdir()
        os.environ.update({
            "WIKI_PATH": str(cls.wiki), "MEMORY_HUB_STAGING": str(cls.tmp / "staging"),
            "MEMORY_HUB_DATA": str(cls.data),
            "MEMORY_HUB_EXPERIENCE_DB": str(cls.data / "experience.sqlite3")})
        git(cls.wiki, "init", "-q")
        git(cls.wiki, "config", "user.email", "t@example.invalid")
        git(cls.wiki, "config", "user.name", "test")
        for n in ("a.md", "b.md", "c.md", "d.md"):
            (cls.wiki / "notes" / n).write_text(PAGE, encoding="utf-8")
        git(cls.wiki, "add", "notes/a.md", "notes/b.md", "notes/c.md", "notes/d.md")
        git(cls.wiki, "commit", "-q", "-m", "init")
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        spec = importlib.util.spec_from_file_location("memory_hub_review_hardening_server",
                                                      ROOT / "scripts" / "server.py")
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)
        fam, addr = cls.server.resolve_loopback_bind_target("127.0.0.1", 0)
        cls.httpd = cls.server.create_loopback_server(fam, addr)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def api(self, payload):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/review",
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def ver(self, name):
        return hashlib.sha256((self.wiki / "notes" / name).read_bytes()).hexdigest()

    def test_lock_busy_refuses_write(self):
        lock = self.data / "locks" / "automation.lock"
        lock.mkdir(parents=True)
        (lock / "info.json").write_text(json.dumps(
            {"pid": os.getpid(), "operation_id": "other-op"}), encoding="utf-8")
        try:
            before = (self.wiki / "notes" / "a.md").read_bytes()
            head = git(self.wiki, "rev-parse", "HEAD")
            code, d = self.api({"source": "page", "id": "notes/a.md", "decision": "reject"})
            self.assertEqual(code, 423, d)
            self.assertEqual(d.get("code"), "lock_busy")
            self.assertEqual((self.wiki / "notes" / "a.md").read_bytes(), before)
            self.assertEqual(git(self.wiki, "rev-parse", "HEAD"), head)
        finally:
            shutil.rmtree(lock, ignore_errors=True)

    def test_version_mismatch_returns_409(self):
        before = (self.wiki / "notes" / "b.md").read_bytes()
        code, d = self.api({"source": "page", "id": "notes/b.md", "decision": "approve",
                            "version": "0" * 64})
        self.assertEqual(code, 409, d)
        self.assertEqual(d["current"], self.ver("b.md"))
        self.assertEqual((self.wiki / "notes" / "b.md").read_bytes(), before)
        # 锁在响应写出前已释放：拿到响应后立即断言
        self.assertFalse((self.data / "locks" / "automation.lock").exists())
        code, d = self.api({"source": "page", "id": "notes/b.md", "decision": "approve",
                            "version": self.ver("b.md")})
        self.assertEqual(code, 200, d)

    def test_commit_contains_only_written_file(self):
        # 工作区里有无关的未暂存改动、已暂存改动和未跟踪文件
        (self.wiki / "notes" / "d.md").write_text(PAGE + "dirty\n", encoding="utf-8")
        (self.wiki / "notes" / "untracked.md").write_text("x\n", encoding="utf-8")
        (self.wiki / "notes" / "b.md").write_text(PAGE + "staged\n", encoding="utf-8")
        git(self.wiki, "add", "notes/b.md")
        try:
            code, d = self.api({"source": "page", "id": "notes/c.md", "decision": "reject",
                                "version": self.ver("c.md")})
            self.assertEqual(code, 200, d)
            self.assertEqual(d["commit"], git(self.wiki, "rev-parse", "HEAD"))
            files = git(self.wiki, "show", "--name-only", "--format=", "HEAD").split()
            self.assertEqual(files, ["notes/c.md"])
            self.assertIn("status: rejected", git(self.wiki, "show", "HEAD:notes/c.md"))
            status = git(self.wiki, "status", "--porcelain")
            self.assertIn("M  notes/b.md", status)
            self.assertIn(" M notes/d.md", status)
            self.assertIn("?? notes/untracked.md", status)
        finally:
            git(self.wiki, "reset", "-q", "--", "notes/b.md")

    def outbox(self):
        import sqlite3
        with sqlite3.connect(self.data / "notion-sync.sqlite3") as c:
            return c.execute("SELECT idempotency_key, object_type, object_id, event_type "
                             "FROM outbox ORDER BY id").fetchall()

    def test_missing_version_returns_400(self):
        before = (self.wiki / "notes" / "d.md").read_bytes()
        for bad in ({}, {"version": ""}, {"version": 123}):
            code, d = self.api(dict({"source": "page", "id": "notes/d.md",
                                     "decision": "reject"}, **bad))
            self.assertEqual(code, 400, d)
            self.assertEqual(d.get("code"), "version_required")
        self.assertEqual((self.wiki / "notes" / "d.md").read_bytes(), before)
        self.assertFalse((self.data / "locks" / "automation.lock").exists())

    def test_review_enqueues_idempotent_outbox_event(self):
        (self.wiki / "notes" / "e.md").write_text(PAGE, encoding="utf-8")
        git(self.wiki, "add", "notes/e.md")
        git(self.wiki, "commit", "-q", "-m", "add e")
        v = self.ver("e.md")
        code, d = self.api({"source": "page", "id": "notes/e.md", "decision": "approve",
                            "version": v})
        self.assertEqual(code, 200, d)
        self.assertTrue(d["outbox"]["inserted"])
        key = d["outbox"]["idempotency_key"]
        rows = [r for r in self.outbox() if r[0] == key]
        self.assertEqual(rows, [(key, "page", "notes/e.md", "review.approved")])
        # 同一版本上的同一审批再入队：不新增、不报错
        out = self.server._review_enqueue("page", "notes/e.md", "approve", v, "page", "notes/e.md")
        self.assertEqual(out, {"idempotency_key": key, "inserted": False})
        self.assertEqual(len([r for r in self.outbox() if r[0] == key]), 1)
        # 失败请求不入队
        n = len(self.outbox())
        self.assertEqual(self.api({"source": "page", "id": "notes/e.md", "decision": "reject",
                                   "version": v})[0], 409)
        self.assertEqual(len(self.outbox()), n)

    def test_outbox_failure_returns_500(self):
        (self.wiki / "notes" / "f.md").write_text(PAGE, encoding="utf-8")
        orig = self.server._review_enqueue

        def broken(*a, **k):
            raise RuntimeError("outbox down")
        self.server._review_enqueue = broken
        try:
            code, d = self.api({"source": "page", "id": "notes/f.md", "decision": "reject",
                                "version": self.ver("f.md")})
        finally:
            self.server._review_enqueue = orig
        self.assertEqual(code, 500, d)
        self.assertIn("outbox 入队失败", d["error"])
        self.assertFalse((self.data / "locks" / "automation.lock").exists())


if __name__ == "__main__":
    unittest.main()

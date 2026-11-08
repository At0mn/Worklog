#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作记录程序（零依赖本地版）

仅需 python3 标准库（麒麟 V10 / Linux / macOS 均可运行），无需联网安装任何东西。
用法: python3 main.py   然后在浏览器中访问终端里显示的地址。
"""
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
APP_NAME = "工作记录"
DEFAULT_CATEGORIES = ["告警分析", "项目管理", "日常运维", "培训学习", "其他"]
MAX_UPLOAD = 500 * 1024 * 1024  # 单个文件上限 500MB
DB_LOCK = threading.Lock()


def default_data_dir():
    """按系统标准找「文档」目录，再拼上 工作记录 子目录。
    麒麟/Linux 中文系统一般是 ~/文档（由 XDG 配置指定）。"""
    user_dirs = Path.home() / ".config" / "user-dirs.dirs"
    if user_dirs.exists():
        try:
            for line in user_dirs.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("XDG_DOCUMENTS_DIR="):
                    val = line.split("=", 1)[1].strip().strip('"')
                    val = val.replace("$HOME", str(Path.home()))
                    if val and val != str(Path.home()):
                        return str(Path(val) / APP_NAME)
        except Exception:
            pass
    doc_cn = Path.home() / "文档"
    if doc_cn.exists():
        return str(doc_cn / APP_NAME)
    return str(Path.home() / "Documents" / APP_NAME)


def load_config():
    stored = {}
    cfg_path = BASE_DIR / "config.json"
    if cfg_path.exists():
        try:
            stored = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            stored = {}
    cfg = {
        "data_dir": stored.get("data_dir") or default_data_dir(),
        "categories": stored.get("categories") or list(DEFAULT_CATEGORIES),
        "port": int(stored.get("port") or 8765),
    }
    return cfg


CFG = load_config()
SAVED_DATA_DIR = CFG["data_dir"]  # 未受环境变量覆盖前的数据目录，保存配置时写回
ENV_DIR = os.environ.get("GONGZUOJILU_DIR")
if ENV_DIR:
    CFG["data_dir"] = ENV_DIR
DATA_DIR = Path(CFG["data_dir"]).expanduser()


def save_config():
    out = dict(CFG)
    out["data_dir"] = SAVED_DATA_DIR
    (BASE_DIR / "config.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- 数据层 ----------------

def db_init():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DATA_DIR / "index.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS records(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT NOT NULL,
      date TEXT NOT NULL,
      category TEXT DEFAULT '',
      tags TEXT DEFAULT '',
      content TEXT DEFAULT '',
      folder TEXT NOT NULL,
      created TEXT,
      updated TEXT,
      done INTEGER DEFAULT 0,
      deadline TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS files(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      record_id INTEGER NOT NULL,
      filename TEXT NOT NULL);
    """)
    # 旧数据库补充新字段（已存在则跳过）
    for stmt in ("ALTER TABLE records ADD COLUMN done INTEGER DEFAULT 0",
                 "ALTER TABLE records ADD COLUMN deadline TEXT DEFAULT ''",
                 "ALTER TABLE records ADD COLUMN entries TEXT DEFAULT ''"):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    return conn


DB = db_init()


def safe_name(name, limit=80):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name)).strip().strip(".")
    return name[:limit] or "未命名"


def material_dir(rel_folder):
    """资料直接存放在记录文件夹里（与 record.md 同级）。"""
    return DATA_DIR / rel_folder


def is_material(f):
    """记录文件夹里，除了程序自用的 record.md 和隐藏文件，都算资料。"""
    return f.is_file() and f.name != "record.md" and not f.name.startswith(".")


def unique_folder(year_dir, base):
    p = year_dir / base
    if not p.exists():
        return p
    i = 2
    while (year_dir / ("%s(%d)" % (base, i))).exists():
        i += 1
    return year_dir / ("%s(%d)" % (base, i))


def get_record(rid):
    with DB_LOCK:
        row = DB.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
    return dict(row) if row else None


def write_record_md(rid):
    r = get_record(rid)
    if not r:
        return
    p = DATA_DIR / r["folder"] / "record.md"
    status = "已完成" if r["done"] else "进行中"
    head = ("# %s\n\n- 日期：%s\n- 类别：%s\n- 标签：%s\n- 状态：%s\n- 截止：%s\n\n---\n\n"
            % (r["title"], r["date"], r["category"], r["tags"], status,
               r["deadline"] or "无"))
    try:
        entries = json.loads(r.get("entries") or "[]")
    except Exception:
        entries = []
    if entries:
        body = "\n".join("- [%s] %s" % (e["time"], e["text"]) for e in entries)
    else:
        body = r["content"] or ""
    p.write_text(head + body, encoding="utf-8")


def safe_subfolder(sub):
    """把传入的相对子目录清洗为安全的相对路径；含 .. 返回 None"""
    sub = str(sub or "").replace("\\", "/").strip("/")
    if not sub:
        return ""
    parts = [p for p in sub.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def iter_material_files(md):
    """遍历记录文件夹内所有资料文件（含子目录），产出相对路径；跳过 record.md 和隐藏文件。"""
    for root, dirs, files in os.walk(md):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            p = Path(root) / name
            rel = p.relative_to(md).as_posix()
            if rel == "record.md":
                continue
            yield rel


def index_files(rid):
    r = get_record(rid)
    with DB_LOCK:
        DB.execute("DELETE FROM files WHERE record_id=?", (rid,))
        if r:
            md = material_dir(r["folder"])
            if md.exists():
                for rel in iter_material_files(md):
                    DB.execute("INSERT INTO files(record_id,filename) VALUES(?,?)",
                               (rid, rel))
        DB.commit()


def reindex_all():
    with DB_LOCK:
        DB.execute("DELETE FROM files")
        rows = DB.execute("SELECT id, folder FROM records").fetchall()
        for r in rows:
            md = material_dir(r["folder"])
            if md.exists():
                for rel in iter_material_files(md):
                    DB.execute("INSERT INTO files(record_id,filename) VALUES(?,?)",
                               (r["id"], rel))
        DB.commit()


def migrate_folders():
    """旧版把资料放在记录文件夹的「资料」子目录里，启动时自动上移到记录文件夹根下。"""
    with DB_LOCK:
        rows = DB.execute("SELECT folder FROM records").fetchall()
    for r in rows:
        base = DATA_DIR / r["folder"]
        old = base / "资料"
        if not old.is_dir():
            continue
        for f in list(old.iterdir()):
            if f.is_file():
                target = base / f.name
                if target.exists():
                    stem, suf = os.path.splitext(f.name)
                    i = 2
                    while (base / ("%s(%d)%s" % (stem, i, suf))).exists():
                        i += 1
                    target = base / ("%s(%d)%s" % (stem, i, suf))
                f.rename(target)
        try:
            old.rmdir()
        except OSError:
            pass  # 里面还有子文件夹等残留，保留不动


def create_record(title, date, category, tags, content, done=0, deadline=""):
    title = (title or "").strip() or "未命名"
    date = date or datetime.now().strftime("%Y-%m-%d")
    year_dir = DATA_DIR / date[:4]
    year_dir.mkdir(parents=True, exist_ok=True)
    folder = unique_folder(year_dir, "%s %s" % (date, safe_name(title)))
    folder.mkdir()
    rel = folder.relative_to(DATA_DIR).as_posix()
    now_dt = datetime.now()
    now = now_dt.isoformat(timespec="seconds")
    entries = []
    if content:
        # 初始内容的时间戳用记录日期（补记过去的工作也能落到正确的周）
        entries = [{"time": "%s %s" % (date, now_dt.strftime("%H:%M")),
                    "text": content}]
    with DB_LOCK:
        cur = DB.execute(
            "INSERT INTO records(title,date,category,tags,content,folder,created,updated,done,deadline,entries)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (title, date, category or "", tags or "", content or "", rel, now, now,
             1 if done else 0, deadline or "",
             json.dumps(entries, ensure_ascii=False)))
        DB.commit()
        rid = cur.lastrowid
    write_record_md(rid)
    index_files(rid)
    return rid


def update_record(rid, title, date, category, tags, content, done=0, deadline=""):
    r = get_record(rid)
    if not r:
        return False
    title = (title or "").strip() or r["title"]
    date = date or r["date"]
    if r["entries"]:
        # 已有时间戳条目时，正文只能通过「追加更新」维护，保持与条目一致
        content = r["content"]
    else:
        content = content or ""
    old_folder = DATA_DIR / r["folder"]
    new_rel = r["folder"]
    desired = "%s/%s %s" % (date[:4], date, safe_name(title))
    if r["folder"] != desired:
        year_dir = DATA_DIR / date[:4]
        year_dir.mkdir(parents=True, exist_ok=True)
        target = unique_folder(year_dir, "%s %s" % (date, safe_name(title)))
        if old_folder.exists():
            old_folder.rename(target)
        else:
            target.mkdir(parents=True, exist_ok=True)
        new_rel = target.relative_to(DATA_DIR).as_posix()
    now = datetime.now().isoformat(timespec="seconds")
    with DB_LOCK:
        DB.execute(
            "UPDATE records SET title=?,date=?,category=?,tags=?,content=?,folder=?,updated=?,done=?,deadline=?"
            " WHERE id=?",
            (title, date, category or "", tags or "", content or "", new_rel, now,
             1 if done else 0, deadline or "", rid))
        DB.commit()
    write_record_md(rid)
    index_files(rid)
    return True


def append_entry(rid, text, legacy=""):
    """追加一条带时间戳的更新；首次追加时把旧正文转为第一条。任务日期自动变为今天。"""
    r = get_record(rid)
    if not r or not (text or "").strip():
        return False
    try:
        entries = json.loads(r["entries"] or "[]")
    except Exception:
        entries = []
    if not entries and (legacy or "").strip():
        base = (r["updated"] or "")[:16] or datetime.now().strftime("%Y-%m-%d %H:%M")
        entries.append({"time": base, "text": legacy.strip()})
    now_dt = datetime.now()
    entries.append({"time": now_dt.strftime("%Y-%m-%d %H:%M"), "text": text.strip()})
    content = "\n".join(e["text"] for e in entries)
    now_iso = now_dt.isoformat(timespec="seconds")
    today = now_dt.strftime("%Y-%m-%d")
    # 文件夹名跟随新日期
    old_folder = DATA_DIR / r["folder"]
    new_rel = r["folder"]
    desired = "%s/%s %s" % (today[:4], today, safe_name(r["title"]))
    if r["folder"] != desired:
        year_dir = DATA_DIR / today[:4]
        year_dir.mkdir(parents=True, exist_ok=True)
        target = unique_folder(year_dir, "%s %s" % (today, safe_name(r["title"])))
        if old_folder.exists():
            old_folder.rename(target)
        else:
            target.mkdir(parents=True, exist_ok=True)
        new_rel = target.relative_to(DATA_DIR).as_posix()
    with DB_LOCK:
        DB.execute(
            "UPDATE records SET entries=?, content=?, updated=?, date=?, folder=?"
            " WHERE id=?",
            (json.dumps(entries, ensure_ascii=False), content, now_iso, today,
             new_rel, rid))
        DB.commit()
    write_record_md(rid)
    index_files(rid)
    return True


def delete_entry(rid, index):
    """删除某条更新（按序号）。"""
    r = get_record(rid)
    if not r:
        return False
    try:
        entries = json.loads(r["entries"] or "[]")
    except Exception:
        entries = []
    try:
        index = int(index)
    except Exception:
        return False
    if index < 0 or index >= len(entries):
        return False
    entries.pop(index)
    content = "\n".join(e["text"] for e in entries)
    now = datetime.now().isoformat(timespec="seconds")
    with DB_LOCK:
        DB.execute(
            "UPDATE records SET entries=?, content=?, updated=? WHERE id=?",
            (json.dumps(entries, ensure_ascii=False), content, now, rid))
        DB.commit()
    write_record_md(rid)
    index_files(rid)
    return True


def delete_record(rid, delete_files):
    r = get_record(rid)
    if not r:
        return False
    with DB_LOCK:
        DB.execute("DELETE FROM files WHERE record_id=?", (rid,))
        DB.execute("DELETE FROM records WHERE id=?", (rid,))
        DB.commit()
    if delete_files:
        shutil.rmtree(DATA_DIR / r["folder"], ignore_errors=True)
    return True


def search_records(kw, category, start, end, done=""):
    sql = ("SELECT DISTINCT r.* FROM records r"
           " LEFT JOIN files f ON f.record_id=r.id WHERE 1=1")
    params = []
    if kw:
        like = "%" + kw + "%"
        sql += (" AND (r.title LIKE ? OR r.content LIKE ? OR r.tags LIKE ?"
                " OR r.category LIKE ? OR f.filename LIKE ?)")
        params += [like] * 5
    if category:
        sql += " AND r.category=?"
        params.append(category)
    if start:
        sql += " AND r.date>=?"
        params.append(start)
    if end:
        sql += " AND r.date<=?"
        params.append(end)
    if done in ("0", "1"):
        sql += " AND r.done=?"
        params.append(int(done))
    sql += " ORDER BY r.date DESC, r.id DESC"
    with DB_LOCK:
        rows = [dict(x) for x in DB.execute(sql, params).fetchall()]
        filehits = {}
        if kw:
            like = "%" + kw + "%"
            filehits = {x["record_id"]: x["n"] for x in DB.execute(
                "SELECT record_id, COUNT(*) n FROM files WHERE filename LIKE ?"
                " GROUP BY record_id", (like,))}
    for r in rows:
        r["match_title"] = r["match_tags"] = r["match_content"] = False
        r["match_files"] = filehits.get(r["id"], 0) if kw else 0
        if kw:
            low = kw.lower()
            r["match_title"] = low in r["title"].lower()
            r["match_tags"] = low in r["tags"].lower()
            r["match_content"] = low in r["content"].lower()
        r.pop("content", None)
    return rows


def get_reminders():
    """未完成且设置了截止日期的记录：已超期的 + 3天内到期的。"""
    today_s = datetime.now().date()
    soon_s = today_s + timedelta(days=3)
    with DB_LOCK:
        rows = [dict(x) for x in DB.execute(
            "SELECT id,title,deadline FROM records"
            " WHERE done=0 AND deadline!='' AND deadline<=?",
            (soon_s.isoformat(),)).fetchall()]
    t = today_s.isoformat()
    overdue = [r for r in rows if r["deadline"] < t]
    soon = [r for r in rows if t <= r["deadline"] <= soon_s.isoformat()]
    return {"overdue": overdue, "soon": soon}


def list_files(rid, sub=""):
    """列出记录文件夹（或其中某个子目录）下的文件夹和文件。"""
    out = {"files": [], "folders": []}
    r = get_record(rid)
    if not r:
        return out
    base = material_dir(r["folder"])
    if sub:
        s = safe_subfolder(sub)
        if s is None:
            return out
        base = base / s
    if not base.is_dir():
        return out
    for f in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name)):
        if f.name.startswith("."):
            continue
        if f.is_dir():
            out["folders"].append({"name": f.name})
        elif f.is_file():
            if not sub and f.name == "record.md":
                continue
            st = f.stat()
            out["files"].append({
                "name": f.name,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    return out


def open_material(rid, rel):
    """用系统默认程序打开某个资料文件（浏览器和服务在同一台机器上）。"""
    r = get_record(rid)
    if not r:
        return {"ok": False, "msg": "记录不存在"}
    root = material_dir(r["folder"]).resolve()
    rel = safe_subfolder(urllib.parse.unquote(rel))
    if rel is None:
        return {"ok": False, "msg": "非法路径"}
    p = (root / rel).resolve()
    if not str(p).startswith(str(root)) or not p.is_file():
        return {"ok": False, "msg": "文件不存在"}
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def open_folder(rid=None):
    r = get_record(rid) if rid else None
    if rid and not r:
        return {"ok": False, "msg": "记录不存在"}
    target = material_dir(r["folder"]) if r else DATA_DIR
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


# ---------------- 导出总结 ----------------

def count_between(start, end):
    with DB_LOCK:
        row = DB.execute("SELECT COUNT(*) c FROM records WHERE date>=? AND date<=?",
                         (start, end)).fetchone()
    return row["c"]


def export_info(start, end):
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    iso_y, iso_w, _ = monday.isocalendar()
    with DB_LOCK:
        year_counts = {x["y"]: x["c"] for x in DB.execute(
            "SELECT substr(date,1,4) y, COUNT(*) c FROM records"
            " GROUP BY y ORDER BY y DESC").fetchall()}
    info = {
        "week": {
            "label": "%d年第%d周（%s ~ %s）" % (iso_y, iso_w,
                                              monday.isoformat()[5:],
                                              sunday.isoformat()[5:]),
            "start": monday.isoformat(),
            "end": sunday.isoformat(),
            "count": count_between(monday.isoformat(), sunday.isoformat()),
        },
        "year_counts": year_counts,
    }
    if start and end:
        info["range"] = {"label": "%s ~ %s" % (start, end),
                         "count": count_between(start, end)}
    else:
        info["range"] = None
    return info


def build_export(scope, start, end, year, dedup):
    if scope == "year":
        start = "%s-01-01" % year
        end = "%s-12-31" % year
    with DB_LOCK:
        rows = [dict(x) for x in DB.execute(
            "SELECT date,title,category,content,entries FROM records"
            " WHERE date>=? AND date<=?"
            " ORDER BY date ASC, id ASC", (start, end)).fetchall()]
    seen = set()
    items = []
    for r in rows:
        cat = ("【%s】" % r["category"]) if r["category"] else ""
        try:
            entries = json.loads(r["entries"] or "[]")
        except Exception:
            entries = []
        if entries:
            # 只导出该时间段内追加的更新；该周期没有新进展的任务整体不出现
            picked = [e for e in entries
                      if start <= (e.get("time") or "")[:10] <= end]
            if not picked:
                continue
            parts = []
            for e in picked:
                t = re.sub(r"\s+", " ", e.get("text") or "").strip()
                if t:
                    parts.append("[%s] %s" % ((e.get("time") or "")[5:16], t))
            body = "；".join(parts)
        else:
            body = re.sub(r"\s+", " ", r["content"] or "").strip()
        if dedup:
            if r["title"] in seen:
                continue
            seen.add(r["title"])
        line = "%s  %s%s" % (r["date"][5:], r["title"], cat)
        if body:
            line += " ｜ " + body
        items.append((r["date"], line))
    bar = "=" * 40
    if scope == "year":
        lines = ["%s年度工作记录汇总" % year, bar]
        cur_month = None
        for date, line in items:
            m = int(date[5:7])
            if m != cur_month:
                cur_month = m
                lines.append("")
                lines.append("【%d月】" % m)
            lines.append(line)
        lines += ["", bar,
                  "全年共 %d 项%s" % (len(items), "（已按标题去重）" if dedup else "")]
        filename = "%s年度工作记录汇总.txt" % year
    else:
        if scope == "week":
            monday = datetime.strptime(start, "%Y-%m-%d").date()
            iso_y, iso_w, _ = monday.isocalendar()
            title = "%d年第%d周工作记录（%s ~ %s）" % (iso_y, iso_w, start[5:], end[5:])
            filename = "%d年第%d周工作记录.txt" % (iso_y, iso_w)
        else:
            title = "工作记录（%s ~ %s）" % (start, end)
            filename = "工作记录 %s至%s.txt" % (start, end)
        lines = [title, bar] + [line for _, line in items]
        lines += [bar, "共 %d 项%s" % (len(items), "（已按标题去重）" if dedup else "")]
    return filename, "\n".join(lines) + "\n"


# ---------------- HTTP 服务 ----------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        path = Path(path)
        if not path.is_file():
            return self._send_json({"error": "文件不存在"}, 404)
        data = path.read_bytes()
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        try:
            self.route_get()
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            self.route_post()
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_PUT(self):
        try:
            self.route_put()
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_DELETE(self):
        try:
            self.route_delete()
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def route_get(self):
        u = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(u.path)
        q = urllib.parse.parse_qs(u.query)
        if path in ("/", "/index.html"):
            return self._send_file(STATIC_DIR / "index.html")
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/static/"):
            full = (STATIC_DIR / path[len("/static/"):]).resolve()
            if full.is_file() and str(full).startswith(str(STATIC_DIR.resolve())):
                return self._send_file(full)
            return self._send_json({"error": "forbidden"}, 403)
        if path == "/api/config":
            return self._send_json({
                "data_dir": str(DATA_DIR), "categories": CFG["categories"]})
        if path == "/api/records":
            rows = search_records(
                q.get("kw", [""])[0].strip(),
                q.get("category", [""])[0],
                q.get("start", [""])[0],
                q.get("end", [""])[0],
                q.get("done", [""])[0])
            return self._send_json({"records": rows, "total": len(rows)})
        if path == "/api/reminders":
            return self._send_json(get_reminders())
        if path == "/api/export/info":
            return self._send_json(export_info(
                q.get("start", [""])[0], q.get("end", [""])[0]))
        if path == "/api/export":
            scope = q.get("scope", ["week"])[0]
            dedup = q.get("dedup", ["0"])[0] == "1"
            year = None
            start = q.get("start", [""])[0]
            end = q.get("end", [""])[0]
            if scope == "year":
                year = q.get("year", [str(datetime.now().year)])[0]
            elif scope == "week" and (not start or not end):
                today = datetime.now().date()
                mon = today - timedelta(days=today.weekday())
                start = mon.isoformat()
                end = (mon + timedelta(days=6)).isoformat()
            start = start or "0000-01-01"
            end = end or "9999-12-31"
            filename, text = build_export(scope, start, end, year, dedup)
            out_dir = DATA_DIR / "导出"
            out_dir.mkdir(exist_ok=True)
            (out_dir / filename).write_text(text, encoding="utf-8")
            data = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
            self.end_headers()
            self.wfile.write(data)
            return
        m = re.fullmatch(r"/api/records/(\d+)", path)
        if m:
            rec = get_record(int(m.group(1)))
            if rec:
                try:
                    rec["entries"] = json.loads(rec.get("entries") or "[]")
                except Exception:
                    rec["entries"] = []
            return self._send_json({"record": rec})
        m = re.fullmatch(r"/api/records/(\d+)/files", path)
        if m:
            rid = int(m.group(1))
            index_files(rid)
            return self._send_json(list_files(rid, q.get("sub", [""])[0]))
        m = re.fullmatch(r"/api/records/(\d+)/files/(.+)", path)
        if m:
            return self.serve_material(int(m.group(1)), m.group(2))
        self._send_json({"error": "not found"}, 404)

    def serve_material(self, rid, name):
        r = get_record(rid)
        if not r:
            return self._send_json({"error": "记录不存在"}, 404)
        root = material_dir(r["folder"]).resolve()
        rel = safe_subfolder(urllib.parse.unquote(name))
        if rel is None:
            return self._send_json({"error": "forbidden"}, 403)
        p = (root / rel).resolve()
        if not str(p).startswith(str(root)):
            return self._send_json({"error": "forbidden"}, 403)
        self._send_file(p)

    def route_post(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path == "/api/records":
            d = self._read_json()
            rid = create_record(d.get("title"), d.get("date"),
                                d.get("category"), d.get("tags"), d.get("content"),
                                d.get("done", 0), d.get("deadline", ""))
            return self._send_json({"id": rid})
        m = re.fullmatch(r"/api/records/(\d+)/files", path)
        if m:
            rid = int(m.group(1))
            r = get_record(rid)
            if not r:
                return self._send_json({"error": "记录不存在"}, 404)
            raw_name = urllib.parse.unquote(self.headers.get("X-Filename", "file"))
            fname = safe_name(os.path.basename(raw_name), limit=120)
            sub = safe_subfolder(
                urllib.parse.unquote(self.headers.get("X-Subfolder", "")))
            if sub is None:
                return self._send_json({"error": "非法路径"}, 400)
            md = material_dir(r["folder"])
            if sub:
                md = md / sub
            md.mkdir(parents=True, exist_ok=True)
            target = md / fname
            if target.exists():
                stem, suf = os.path.splitext(fname)
                i = 2
                while (md / ("%s(%d)%s" % (stem, i, suf))).exists():
                    i += 1
                target = md / ("%s(%d)%s" % (stem, i, suf))
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_UPLOAD:
                return self._send_json({"error": "文件过大"}, 413)
            with open(target, "wb") as fp:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    fp.write(chunk)
                    remaining -= len(chunk)
            index_files(rid)
            return self._send_json({"ok": True, "name": target.name})
        m = re.fullmatch(r"/api/records/(\d+)/entries", path)
        if m:
            d = self._read_json()
            ok = append_entry(int(m.group(1)), d.get("text", ""), d.get("legacy", ""))
            return self._send_json({"ok": ok})
        m = re.fullmatch(r"/api/records/(\d+)/mkdir", path)
        if m:
            d = self._read_json()
            r = get_record(int(m.group(1)))
            if not r:
                return self._send_json({"error": "记录不存在"}, 404)
            sub = safe_subfolder(d.get("sub", ""))
            name = safe_name(d.get("name", ""), limit=60)
            if sub is None or not name:
                return self._send_json({"error": "非法名称"}, 400)
            base = material_dir(r["folder"])
            target = base / sub / name if sub else base / name
            target.mkdir(parents=True, exist_ok=True)
            index_files(int(m.group(1)))
            return self._send_json({"ok": True})
        m = re.fullmatch(r"/api/records/(\d+)/open_file", path)
        if m:
            d = self._read_json()
            return self._send_json(open_material(int(m.group(1)), d.get("path", "")))
        m = re.fullmatch(r"/api/records/(\d+)/open_folder", path)
        if m:
            return self._send_json(open_folder(int(m.group(1))))
        self._send_json({"error": "not found"}, 404)

    def route_put(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        m = re.fullmatch(r"/api/records/(\d+)", path)
        if m:
            d = self._read_json()
            ok = update_record(int(m.group(1)), d.get("title"), d.get("date"),
                               d.get("category"), d.get("tags"), d.get("content"),
                               d.get("done", 0), d.get("deadline", ""))
            return self._send_json({"ok": ok})
        if path == "/api/config":
            d = self._read_json()
            cats = d.get("categories")
            if isinstance(cats, list):
                CFG["categories"] = [str(c) for c in cats if str(c).strip()]
                save_config()
            return self._send_json({"ok": True, "categories": CFG["categories"]})
        self._send_json({"error": "not found"}, 404)

    def route_delete(self):
        u = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(u.path)
        q = urllib.parse.parse_qs(u.query)
        m = re.fullmatch(r"/api/records/(\d+)", path)
        if m:
            delete_files = q.get("delete_files", ["0"])[0] == "1"
            return self._send_json({"ok": delete_record(int(m.group(1)), delete_files)})
        m = re.fullmatch(r"/api/records/(\d+)/entries/(\d+)", path)
        if m:
            return self._send_json({"ok": delete_entry(int(m.group(1)), m.group(2))})
        m = re.fullmatch(r"/api/records/(\d+)/files/(.+)", path)
        if m:
            rid = int(m.group(1))
            r = get_record(rid)
            if not r:
                return self._send_json({"error": "记录不存在"}, 404)
            root = material_dir(r["folder"]).resolve()
            rel = safe_subfolder(m.group(2))
            if rel is None:
                return self._send_json({"error": "forbidden"}, 403)
            p = (root / rel).resolve()
            if not str(p).startswith(str(root)):
                return self._send_json({"error": "forbidden"}, 403)
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            index_files(rid)
            return self._send_json({"ok": True})
        self._send_json({"error": "not found"}, 404)


def pick_port(preferred):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", preferred))
        s.close()
        return preferred
    except OSError:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    migrate_folders()
    reindex_all()
    port = pick_port(int(CFG.get("port") or 8765))
    url = "http://127.0.0.1:%d" % port
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    print("=" * 50)
    print("  %s 已启动" % APP_NAME)
    print("  数据目录: %s" % DATA_DIR)
    print("  浏览器访问: %s" % url)
    print("  停止: 在本终端按 Ctrl+C")
    print("=" * 50)
    if not os.environ.get("GONGZUOJILU_NO_BROWSER"):
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()

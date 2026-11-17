#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键启动器（双击「启动」脚本调用的就是这个文件）。

只做四件事：
  1. 切到本文件所在目录（这样双击也能找到 web/ 和 rules/）
  2. 找一个没被占用的端口
  3. 自动打开浏览器
  4. 交给 server.py

不装任何第三方包，Python 3.7+ 自带的能力就够了。
Windows 上控制台默认是 GBK，输出里只要有 → 这种符号就会抛
UnicodeEncodeError，所以在最开头就把 stdout 改成 UTF-8。
"""
import os
import socket
import sys
import threading
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))

# 输出编码先处理，后面任何一行 print 都不会因为字符集崩掉
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

MIN_PY = (3, 7)


def _free_port(start=8877):
    for p in range(start, start + 40):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0


def _die(msg):
    print("\n  " + msg.replace("\n", "\n  ") + "\n")
    try:
        input("  按回车键关闭…")
    except EOFError:
        pass
    return 1


def _preflight():
    """缺文件是最常见的「双击没反应」，这里先自己说清楚。"""
    must = [("web", "index.html"), ("scripts", "server.py"),
            ("scripts", "session.py"), ("build", "_actions.json")]
    missing = [os.path.join(d, f) for d, f in must
               if not os.path.isfile(os.path.join(ROOT, d, f))]
    if missing:
        return "压缩包没有解压完整，缺少：\n" + "、".join(missing) + \
               "\n请把整个文件夹重新解压一次，不要只把压缩包里的单个文件拖出来。"
    return None


def main():
    if sys.version_info < MIN_PY:
        return _die("需要 Python %d.%d 或更新版本，当前是 %s。\n"
                    "请到 https://www.python.org/downloads/ 装一个 3.8 以上的版本。"
                    % (MIN_PY[0], MIN_PY[1], sys.version.split()[0]))

    bad = _preflight()
    if bad:
        return _die(bad)

    try:
        port = _free_port(int(os.environ.get("PORT") or 8877))
    except ValueError:
        port = _free_port()
    if not port:
        return _die("8877 附近 40 个端口都被占用了。\n"
                    "关掉一些程序再试，或者手动运行：python start.py")

    url = "http://127.0.0.1:%d" % port
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    print("\n  正在启动… 页面会自动打开；没打开就手动访问 %s" % url)
    print("  （这个窗口要留着，关掉服务就停了）\n")

    sys.argv = ["server.py", "--port", str(port), "--quiet"]
    try:
        import server
    except Exception as e:
        return _die("加载引擎失败：%s: %s" % (type(e).__name__, e))
    try:
        server.main()
    except OSError as e:
        return _die("端口 %d 起不来：%s\n换个端口试试：python start.py" % (port, e))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass

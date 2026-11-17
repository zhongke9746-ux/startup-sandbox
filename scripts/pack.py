#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把项目打成一个「别人解压就能用」的包。

    python3 scripts/pack.py            # 生成 dist/创业沙盒-<日期>.zip

只做分发需要的东西：
  · 砍掉开发脚本、十几份设计文档、你自己的会话历史
  · 带上预编译好的 build/*.json（收包的人不需要 ruby）
  · 附两个示例会话，开箱能看到跑完的样子
  · 生成 zip 时给启动脚本写上可执行权限（macOS 双击才不报错）
"""
import datetime
import json
import os
import shutil
import stat
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

# 运行时真正需要的模块；其余是校准/验证用的开发脚本，不进包
RUNTIME = ["engine.py", "session.py", "api.py", "server.py",
           "intake.py", "monte_carlo.py", "fetch.py", "yaml2json.rb"]

def _demo_sessions():
    """示例会话清单放在 sessions/.demos（该目录整体不入库）。

    会话是用户本地数据，开源仓库里一个都不带；想给分发 zip 附示例时，
    在本地建 sessions/.demos 写入文件名即可。
    """
    mf = os.path.join(ROOT, "sessions", ".demos")
    if os.path.isfile(mf):
        return [l.strip() for l in open(mf, encoding="utf-8")
                if l.strip().endswith(".json")]
    return []

NAME = "创业沙盒"


def _rm(p):
    if os.path.isdir(p):
        shutil.rmtree(p)
    elif os.path.exists(p):
        os.remove(p)


def _copy_text(src, dst):
    """按目标平台规范化换行符。

    .bat 必须是 CRLF：cmd.exe 对只有 LF 的批处理解析会错乱，
    带 if (...) 块的更是直接失败，表现是双击后窗口一闪就没了。
    .command / .py / .txt 保持 LF。
    """
    raw = open(src, "rb").read()
    text = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if dst.endswith(".bat"):
        text = text.replace(b"\n", b"\r\n")
    with open(dst, "wb") as f:
        f.write(text)


def check_launchers(out):
    """打完之后自己验一遍，别让「换行符」这种问题再流出去。"""
    bad = []
    bat = os.path.join(out, "启动-Windows.bat")
    if os.path.isfile(bat):
        raw = open(bat, "rb").read()
        lf = raw.count(b"\n")
        crlf = raw.count(b"\r\n")
        if lf != crlf:
            bad.append("启动-Windows.bat 有 %d 处是裸 LF（共 %d 行）" % (lf - crlf, lf))
        if raw[:1] == b"\xef" or any(b > 127 for b in raw):
            bad.append("启动-Windows.bat 含非 ASCII 字符，老系统上会乱码")
    cmd = os.path.join(out, "启动-macOS.command")
    if os.path.isfile(cmd):
        raw = open(cmd, "rb").read()
        if b"\r\n" in raw:
            bad.append("启动-macOS.command 混入了 CRLF，bash 会报 bad interpreter")
    return bad


def stage(out):
    _rm(out)
    os.makedirs(out)

    # 1. 启动器与说明
    for f in ["启动-macOS.command", "启动-Windows.bat", "使用说明.txt"]:
        _copy_text(os.path.join(ROOT, "dist-src", f), os.path.join(out, f))
    _copy_text(os.path.join(ROOT, "start.py"), os.path.join(out, "start.py"))

    # 2. 运行时代码
    os.makedirs(os.path.join(out, "scripts"))
    for f in RUNTIME:
        shutil.copy2(os.path.join(ROOT, "scripts", f), os.path.join(out, "scripts", f))

    # 3. 规则（yaml 留作可改，json 是实际读的）
    for d in ["rules", "build", "web", "snapshots"]:
        if os.path.isdir(os.path.join(ROOT, d)):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(out, d))

    # 4. 示例会话 + 空目录
    os.makedirs(os.path.join(out, "sessions"))
    for f in _demo_sessions():
        src = os.path.join(ROOT, "sessions", f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(out, "sessions", f))

    return out


def audit_paths(out):
    """别人的机器上不该出现 /Users/edy 这种绝对路径。"""
    bad = []
    for base, _, files in os.walk(out):
        for f in files:
            if not f.endswith((".py", ".json", ".html", ".txt", ".command", ".bat")):
                continue
            p = os.path.join(base, f)
            try:
                t = open(p, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if "/Users/edy" in t:
                bad.append(os.path.relpath(p, out))
    return bad


def zipit(src, zpath):
    exec_bits = {".command": 0o755, ".sh": 0o755}
    base = os.path.dirname(src)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(src):
            for f in files:
                if f == ".DS_Store":
                    continue
                full = os.path.join(root, f)
                arc = os.path.relpath(full, base)
                zi = zipfile.ZipInfo.from_file(full, arc)
                mode = exec_bits.get(os.path.splitext(f)[1], 0o644)
                zi.external_attr = (stat.S_IFREG | mode) << 16
                zi.compress_type = zipfile.ZIP_DEFLATED
                with open(full, "rb") as fh:
                    z.writestr(zi, fh.read())
    return zpath


def main():
    stamp = datetime.date.today().isoformat()
    dist = os.path.join(ROOT, "dist")
    os.makedirs(dist, exist_ok=True)
    out = stage(os.path.join(dist, NAME))

    bad = audit_paths(out)
    bad += check_launchers(out)
    if bad:
        print("！打包自检未通过，已中止：")
        for b in bad:
            print("   ", b)
        return 1

    n = sum(len(fs) for _, _, fs in os.walk(out))
    size = sum(os.path.getsize(os.path.join(r, f))
               for r, _, fs in os.walk(out) for f in fs)
    z = zipit(out, os.path.join(dist, "%s-%s.zip" % (NAME, stamp)))

    print("打包完成")
    print("  目录  %s   （%d 个文件，%.0f KB）" % (out, n, size / 1024))
    print("  压缩  %s   （%.0f KB）" % (z, os.path.getsize(z) / 1024))
    print("  示例会话  %s" % ("、".join(_demo_sessions()) or "（无）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
import io, sys, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
h = open("static/index.html", encoding="utf-8").read()
css = h[h.index("<style>"):h.index("</style>")]
# 所有 .row 和 .col-side 规则（含位置）
for m in re.finditer(r"\.row\{[^}]*\}|\.col-side\{[^}]*\}|\.col-main\{[^}]*\}", css):
    pos = m.start()
    # 找它是否在 @media 内
    before = css[:pos]
    in_media = before.rfind("@media")
    media_ctx = ""
    if in_media != -1:
        seg = css[in_media:pos]
        if seg.count("{") > seg.count("}"):  # 还在块内
            media_ctx = css[in_media:css.index("{", in_media)].strip()
    line = h[:h.index("<style>")].count("\n") + css[:pos].count("\n") + 9
    print(f"line {line} [{media_ctx or 'GLOBAL'}]: {m.group(0)[:90]}")

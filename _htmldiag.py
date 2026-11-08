# -*- coding: utf-8 -*-
"""HTML 标签配对诊断：找出所有 endtag 与栈顶不匹配的位置。"""
import io, sys
from html.parser import HTMLParser
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

VOID = {"img","br","input","hr","meta","link","area","base","col","embed","source","track","wbr"}

class Diag(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []   # (tag, line)
        self.mismatches = []
    def handle_starttag(self, tag, attrs):
        if tag in VOID: return
        # 特例：<p> 等可自动闭合的省略，这里只关心 div/section/aside/main/header 等
        self.stack.append((tag, self.getpos()[0]))
    def handle_startendtag(self, tag, attrs):
        pass
    def handle_endtag(self, tag):
        if tag in VOID: return
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
            return
        # 不匹配：在栈里从顶往下找同名
        for k in range(len(self.stack)-1, -1, -1):
            if self.stack[k][0] == tag:
                # 被跳过（提前死亡）的
                skipped = self.stack[k+1:]
                for t, ln in skipped:
                    self.mismatches.append(f"line {ln}: <{t}> 被提前闭合（在 </{tag}> line {self.getpos()[0]} 时仍未闭合）")
                del self.stack[k:]
                return
        self.mismatches.append(f"line {self.getpos()[0]}: 多余的 </{tag}>（栈里没有对应的开始标签）")

for path in ["static/index.html", "static/upload.html"]:
    h = open(path, encoding="utf-8").read()
    d = Diag()
    d.feed(h)
    d.close()
    print("=" * 20, path)
    if d.mismatches:
        for m in d.mismatches:
            print(" !", m)
    leftover = [(t, ln) for t, ln in d.stack if t not in ("html", "body")]
    if leftover:
        for t, ln in leftover:
            print(" ! 未闭合: <" + t + "> 开始于 line", ln)
    if not d.mismatches and not leftover:
        print(" 结构完全正确")

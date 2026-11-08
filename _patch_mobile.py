# -*- coding: utf-8 -*-
"""移动端 UI 大修：统一手机断点、修 tabbar 挤压、防横向溢出、侧栏固定定位、右栏下沉。

根因分析：
1. 手机上有 9 个导航项挤在底部 tabbar，每项 flex:1 导致文字压成竖排/溢出 → 改为横向可滚动 tabbar。
2. .app-shell 在手机端仍被桌面规则 grid-template-columns 影响（后来补了 1fr，但 .sidebar 是 sticky/fixed 混合）。
3. .workspace 桌面 padding 18px 24px + max-width 1400，手机上未覆盖 → 内容横向溢出。
4. .row 两栏在手机上 1fr，但 .col-side sticky 相关规则冲突。
5. 多个 @media 块分散且互相覆盖，统一整理。
"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

p = "static/index.html"
s = open(p, encoding="utf-8").read()

# 1) 移除所有分散的 @media (max-width:980px) 块，统一在文件末尾追加一个权威块
import re

# 找出所有 @media (max-width:980px){...} 块（含嵌套大括号，用计数法）
def remove_media_blocks(text):
    marker = "@media (max-width:980px){"
    out = []
    i = 0
    while True:
        j = text.find(marker, i)
        if j == -1:
            out.append(text[i:])
            break
        out.append(text[i:j])
        # 从 { 后开始数括号
        k = j + len(marker)
        depth = 1
        while k < len(text) and depth > 0:
            if text[k] == '{':
                depth += 1
            elif text[k] == '}':
                depth -= 1
            k += 1
        i = k
    return "".join(out)

s = remove_media_blocks(s)

# 2) 追加权威移动端块（放在 </style> 前）
mobile_css = """
  /* ==================== 手机端统一布局（<=980px） ==================== */
  @media (max-width:980px){
    html{-webkit-text-size-adjust:100%}
    body{overflow-x:hidden}
    header{
      padding:10px 14px;position:sticky;top:0;z-index:60;
      gap:10px;flex-wrap:wrap;
    }
    header h1{font-size:15px}
    header .muted, header .sub{display:none}
    main{padding:0 0 72px;min-width:0;max-width:100vw;overflow-x:hidden}
    .app-shell{
      display:block;max-width:100vw;padding:10px 10px 80px;
      margin:0;min-width:0;
    }
    /* 底部导航：横向滚动，9 项不再挤成竖排 */
    .sidebar{
      position:fixed;bottom:0;left:0;right:0;z-index:80;
      height:auto;min-height:0;max-height:none;
      flex-direction:row;align-items:stretch;gap:2px;
      padding:6px 6px calc(6px + env(safe-area-inset-bottom));
      border-radius:16px 16px 0 0;border:1px solid var(--line);border-bottom:none;
      background:var(--header-bg);backdrop-filter:blur(16px);
      box-shadow:0 -6px 24px rgba(0,0,0,.14);
      overflow-x:auto;overflow-y:hidden;
      scrollbar-width:none;-webkit-overflow-scrolling:touch;
    }
    .sidebar::-webkit-scrollbar{display:none}
    .sidebar .brand,.sidebar .side-quote,.sidebar .side-sep,.sidebar .side-note{display:none}
    .sidebar .nav{
      flex:0 0 auto;flex-direction:column;gap:2px;align-items:center;justify-content:center;
      width:auto;min-width:62px;min-height:54px;padding:6px 8px;
      border-radius:12px;font-size:10.5px;white-space:nowrap;
    }
    .sidebar .nav .ico{width:auto;font-size:19px;line-height:1.1}
    .sidebar .nav .t{font-size:10.5px;font-weight:500;white-space:nowrap}
    .sidebar .nav .dotmark{display:none}
    .sidebar .nav.active{box-shadow:none;background:rgba(56,189,248,.16);color:var(--pink)}
    .sidebar .side-foot{margin:0;display:contents}
    /* 内容区 */
    .workspace{min-width:0;max-width:100vw;padding:0;overflow:hidden}
    .row{display:block;min-width:0}
    .col-main{display:block;min-width:0}
    .col-main .view{display:none}
    .col-main .view.active{display:block}
    .col-side{
      position:static;max-height:none;overflow:visible;
      margin-top:14px;
    }
    .panel{padding:14px 12px;border-radius:16px;min-width:0;overflow:hidden}
    .panel h2{font-size:13.5px;flex-wrap:wrap;gap:6px}
    .panel h2 .small{padding:6px 12px;font-size:12px}
    .metrics-grid{grid-template-columns:1fr}
    .m-strip{grid-template-columns:repeat(2,1fr)}
    .statgrid{grid-template-columns:repeat(2,1fr)}
    .tasklist{grid-template-columns:1fr;gap:10px}
    .tasklist .t{padding:12px}
    .tasklist .ops{max-width:none;width:100%;justify-content:flex-end;flex-wrap:wrap}
    .tasklist .ops .small{padding:8px 14px;font-size:12.5px}
    .tasklist .exec{width:56px;height:56px}
    .imgs{grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px}
    .log{max-height:280px;font-size:11.5px}
    #sec-logs.active{height:auto}
    #sec-logs .log{max-height:60vh}
    dialog{width:100vw;max-height:100dvh;height:100dvh;border-radius:0;max-width:none}
    .dlg-body{max-height:calc(100dvh - 120px);padding:14px}
    .grid2,.grid3{grid-template-columns:1fr}
    .cronrow{grid-template-columns:1fr 1fr}
    .cronrow input{grid-column:span 2}
    input,select,textarea{font-size:16px;max-width:100%}
    button{min-height:38px}
    .toast{bottom:calc(72px + 12px)}
    .side-quote{display:none}
    .m-card{min-width:0}
    .m-strip .cell{min-width:0}
  }
  @media (max-width:400px){
    .m-strip{grid-template-columns:1fr 1fr}
    .sidebar .nav{min-width:56px;padding:6px 5px}
  }
"""
s = s.replace("</style>", mobile_css + "\n</style>", 1)

open(p, "w", encoding="utf-8").write(s)
print("mobile CSS rewritten | media blocks left:", s.count("@media (max-width:980px)"))

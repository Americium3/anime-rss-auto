# -*- coding: utf-8 -*-
# 只读诊断 mark-watched：为什么暂停没触发。不写 bgm、不动 seed_states.json。
import anime_rss as A

torrents = A.qb_get_json("/api/v2/torrents/info")
old = A.load_seed_states()
print(f"qB 种子数={len(torrents)}  基线(seed_states.json)记录数={len(old)}")
print(f"_SEEDING_STATES={sorted(A._SEEDING_STATES)}")
print(f"_STOPPED_UP_STATES={sorted(A._STOPPED_UP_STATES)}\n")

# 当前正处于"做种态"的种子（这些才可能在下次暂停时被捕捉到跳变）
seeding_now = [t for t in torrents if t.get("state") in A._SEEDING_STATES]
print(f"== 当前在做种态的种子(下次暂停可被捕捉) = {len(seeding_now)} ==")
for t in seeding_now[:30]:
    print(f"   {t.get('state'):10} {t.get('name','')[:60]}")

# 已停+完成的种子：看它们"上一轮"是什么状态——若上一轮已是 stopped，则永远不会触发
stopped_done = [t for t in torrents if t.get("state") in A._STOPPED_UP_STATES and t.get("progress",0)>=1]
print(f"\n== 已停且完成的种子 = {len(stopped_done)} ==（看 prev 列：只有 prev∈做种态 才会触发）")
fire_candidates = 0
for t in stopped_done:
    prev = old.get(t["hash"], "<不在基线>")
    will = "★会触发" if prev in A._SEEDING_STATES else "—不触发(上轮已停或新种)"
    if prev in A._SEEDING_STATES: fire_candidates += 1
    # 只打印少量，避免刷屏
print(f"   其中『上一轮在做种态、本轮已停』= {fire_candidates} 个（这些才会在下轮真实标记）")

# 逐条只列出 prev 在做种态的（真正会触发的），以及最近改动的几个
print("\n   会触发的明细：")
shown=0
for t in stopped_done:
    prev = old.get(t["hash"], "<不在基线>")
    if prev in A._SEEDING_STATES:
        print(f"     prev={prev:10} now={t.get('state'):10} {t.get('name','')[:55]}")
        shown+=1
if shown==0:
    print("     （无——所以本轮不会标记任何集）")

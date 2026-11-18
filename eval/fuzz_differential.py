"""Differential fuzzer: random policies + mutants, labelled by an INDEPENDENT brute-force evaluator written here
(it shares no code with reviewer/). Checks the 'exhaustive within the supported language' claim empirically.
  python -m eval.fuzz_differential --n 300 --seed 1
Spec and workflow are both generated from the mutant, so every case is fully reviewable (no INCOMPLETE expected)."""
import argparse, copy, json, os, random, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from reviewer.review import review
with open(os.path.join(ROOT, 'fixtures', 'bundles', 'V01.json')) as _f:
    base = json.load(_f)

CATS=["billing","tech","fraud"]; TIERS=["vip","std"]
# condition tree: ("cmp",field,op,val) | ("miss",f) | ("and",[..]) | ("or",[..]) | ("not",x)
def rnd_atom(r):
    k=r.choice(["w","w","u","c","t","cm","tm"])
    if k=="w": return ("cmp","waiting",r.choice(["<","<=",">",">=","==","!="]),r.choice([0,5,10,25,29,30,31,45,60]))
    if k=="u": return ("cmp","urgent","==",r.choice([True,False]))
    if k=="c": return ("cmp","category",r.choice(["==","!="]),r.choice(CATS))
    if k=="t": return ("cmp","tier",r.choice(["==","!="]),r.choice(TIERS))
    if k=="cm": return ("miss","category")
    return ("miss","tier")
def rnd_cond(r,d=0):
    if d>=2 or r.random()<0.45: return rnd_atom(r)
    k=r.choice(["and","or","not"])
    if k=="not": return ("not",rnd_cond(r,d+1))
    return (k,[rnd_cond(r,d+1) for _ in range(r.choice([2,3]))])
def txt(c):
    t=c[0]
    if t=="cmp":
        v=c[3]; v = ("true" if v else "false") if isinstance(v,bool) else (f"{v} min" if c[1]=="waiting" else v)
        return f"{c[1]} {c[2]} {v}"
    if t=="miss": return f"{c[1]} is missing"
    if t=="not": return f"NOT ({txt(c[1])})"
    return "("+(" AND " if t=="and" else " OR ").join(txt(x) for x in c[1])+")"
# independent evaluator, missing => comparisons False (documented contract)
def ev(c,tk):
    t=c[0]
    if t=="cmp":
        v=tk.get(c[1])
        if v is None: return False
        o,k=c[2],c[3]
        return {"<":v<k,"<=":v<=k,">":v>k,">=":v>=k,"==":v==k,"!=":v!=k}[o]
    if t=="miss": return tk.get(c[1]) is None
    if t=="not": return not ev(c[1],tk)
    if t=="and": return all(ev(x,tk) for x in c[1])
    return any(ev(x,tk) for x in c[1])
ROUTES=["manual_review","priority_queue","escalation","normal_queue"]
def route(rules,default,tk):
    for c,rt in rules:
        if ev(c,tk): return rt
    return default
# NOTE: the value domain of categorical fields includes a value that none of the rules mention ("other"); without it the
# brute-force oracle under-approximates the domain and mislabels reviewer counterexamples such as `tier != std` as false blocks.
TICKETS=[{"category":c,"urgent":u,"waiting":w,"tier":t} for c in [None]+CATS+["other"] for u in [None,True,False] for t in [None]+TIERS+["other"] for w in [None]+list(range(0,70))]

def bundle(req_rules, spec_rules, i):
    b=copy.deepcopy(base); b['bundle_id']=f"F{i}"
    b['fields']['category']={"type":"categorical"}; b['fields']['tier']={"type":"categorical"}
    b['requirements']['items']=[{"id":f"R{j}","anchor":f"p#{j}","order":j+1,"text":f"IF {txt(c)} THEN {rt}"} for j,(c,rt) in enumerate(req_rules)]+[{"id":"RD","anchor":"p#d","order":99,"text":"OTHERWISE normal queue"}]
    b['spec']['rules']=[{"id":f"S{j}","source_ref":f"R{j}","when":txt(c),"then":rt} for j,(c,rt) in enumerate(spec_rules)]
    b['spec']['default']="normal_queue"
    # workflow: chain of decision nodes mirroring spec
    nodes=[{"id":"n","version":"v1","kind":"decision","branches":[{"when":txt(c),"to":"t_"+rt} for c,rt in spec_rules]+[{"when":"else","to":"t_normal_queue"}]}]
    nodes+=[{"id":"t_"+rt,"version":"v1","kind":"terminal","route":rt} for rt in ROUTES]
    b['workflow']['nodes']=nodes; b['workflow']['entry']="n"
    b['snippets']=[]
    return b

def mutate(r,rules):
    rules=copy.deepcopy(rules); k=r.choice(["thr","op","swap","drop","route","same"])
    if k=="same": return rules
    if k=="drop" and len(rules)>1: rules.pop(r.randrange(len(rules)))
    elif k=="swap" and len(rules)>1:
        i,j=r.sample(range(len(rules)),2); rules[i],rules[j]=rules[j],rules[i]
    elif k=="route":
        i=r.randrange(len(rules)); rules[i]=(rules[i][0],r.choice(ROUTES))
    else:
        i=r.randrange(len(rules)); rules[i]=(rnd_cond(r),rules[i][1])
    return rules


def run_fuzz(N, seed):
    r = random.Random(seed)
    TD = 0
    stats = {"n": N, "agree": 0, "false_pass": 0, "false_block": 0, "incomplete": 0, "crash": 0}; bad = []
    for i in range(N):
        req = [(rnd_cond(r), r.choice(ROUTES[:3])) for _ in range(r.choice([2, 3, 4]))]
        spec = mutate(r, req)
        true_diff = any(route(req, "normal_queue", t) != route(spec, "normal_queue", t) for t in TICKETS)
        TD += true_diff
        try:
            rep = review(bundle(req, spec, i))
        except Exception as e:
            stats["crash"] += 1; bad.append((i, "crash", repr(e))); continue
        st = rep["status"]
        if st == "INCOMPLETE": stats["incomplete"] += 1; bad.append((i, "incomplete", rep["incomplete_reasons"][:2])); continue
        exp = "CHANGES_REQUIRED" if true_diff else "PASS"
        if st == exp: stats["agree"] += 1
        elif st == "PASS": stats["false_pass"] += 1; bad.append((i, "FALSE_PASS"))
        else: stats["false_block"] += 1; bad.append((i, "FALSE_BLOCK"))
    stats["truly_defective"] = TD
    return stats, bad


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--n", type=int, default=200); ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(); st, bad = run_fuzz(a.n, a.seed); print(json.dumps(st)); [print(x) for x in bad[:5]]
    sys.exit(1 if st["false_pass"] or st["false_block"] or st["crash"] else 0)

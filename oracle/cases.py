"""Independent routing oracle.

Hand-written plain-Python routing functions, one per scenario. They share NO code with
`reviewer/` and are never read by it. Labels and counterexample checks come from here.
A ticket is a dict: category (str|None), urgent (bool|None), waiting (minutes|None), tier (str|None).
`None` returned by an oracle function means "no route"; "<error>" means the code would crash.
"""


def base(t, thr=30):
    if t.get("category") is None:
        return "manual_review"
    if t.get("urgent") is True:
        return "priority_queue"
    w = t.get("waiting")
    if w is not None and w >= thr:
        return "escalation"
    return "normal_queue"


def strict_gt(t):            # waiting > 30 instead of >= 30
    if t.get("category") is None:
        return "manual_review"
    if t.get("urgent") is True:
        return "priority_queue"
    w = t.get("waiting")
    if w is not None and w > 30:
        return "escalation"
    return "normal_queue"


def urgent_first(t):         # R2 before R1
    if t.get("urgent") is True:
        return "priority_queue"
    if t.get("category") is None:
        return "manual_review"
    w = t.get("waiting")
    if w is not None and w >= 30:
        return "escalation"
    return "normal_queue"


def no_r1(t):                # missing-category rule omitted
    if t.get("urgent") is True:
        return "priority_queue"
    w = t.get("waiting")
    if w is not None and w >= 30:
        return "escalation"
    return "normal_queue"


def wrong_dest(t):           # escalation branch wired to the normal queue
    r = base(t)
    return "normal_queue" if r == "escalation" else r


def no_else(t):              # no branch for ordinary tickets
    r = base(t)
    return None if r == "normal_queue" else r


def unit_seconds(t):         # "waiting >= 30" read as 30 seconds => 0.5 minutes
    return base(t, thr=0.5)


def crash_on_missing_waiting(t):   # unguarded `ticket.get("waiting") >= 30`
    if t.get("category") is None:
        return "manual_review"
    if t.get("urgent") is True:
        return "priority_queue"
    w = t.get("waiting")
    if w is None:
        return "<error>"
    return "escalation" if w >= 30 else "normal_queue"


def tier_policy(t):          # R1, R2, R2b (vip & waiting>=10 -> priority), R3
    if t.get("category") is None:
        return "manual_review"
    if t.get("urgent") is True:
        return "priority_queue"
    w = t.get("waiting")
    if t.get("tier") == "vip" and w is not None and w >= 10:
        return "priority_queue"
    if w is not None and w >= 30:
        return "escalation"
    return "normal_queue"


def tier_exception_first(t):  # vip exception placed before the missing-category rule
    w = t.get("waiting")
    if t.get("tier") == "vip" and w is not None and w >= 10:
        return "priority_queue"
    return tier_policy_without_exception(t)


def tier_policy_without_exception(t):
    if t.get("category") is None:
        return "manual_review"
    if t.get("urgent") is True:
        return "priority_queue"
    w = t.get("waiting")
    if w is not None and w >= 30:
        return "escalation"
    return "normal_queue"


def thr(n):
    return lambda t: base(t, n)


# case -> expected (effective policy) and per-artifact actual behaviour where it differs.
CASES = {
    "V01": dict(expected=base, actual={}),
    "V02": dict(expected=thr(25), actual={}),
    "V03": dict(expected=base, actual={}),
    "V04": dict(expected=base, actual={}),
    "V05": dict(expected=base, actual={}),
    "V06": dict(expected=base, actual={}),
    "V07": dict(expected=base, actual={}),
    "D01": dict(expected=base, actual={"spec": thr(35)}),
    "D02": dict(expected=base, actual={"spec": strict_gt}),
    "D03": dict(expected=base, actual={"spec": urgent_first}),
    "D04": dict(expected=base, actual={"spec": no_r1}),
    "D05": dict(expected=thr(25), actual={"spec": thr(30)}),
    "D06": dict(expected=base, actual={"workflow": wrong_dest}),
    "D07": dict(expected=base, actual={"code": strict_gt}),
    "D08": dict(expected=tier_policy, actual={"spec": tier_exception_first}),
    "D09": dict(expected=base, actual={"workflow": no_else}),
    "D10": dict(expected=base, actual={"spec": unit_seconds}),
    "D11": dict(expected=base, actual={"spec": thr(35)}),
    "D12": dict(expected=base, actual={"spec": thr(35)}),
    "D13": dict(expected=base, actual={"code": crash_on_missing_waiting}),
    "D14": dict(expected=base, actual={"spec": thr(35)}),
    # INCOMPLETE cases have no single oracle: the missing information is the point.
}

SWEEP_TICKETS = [
    {"category": c, "urgent": u, "waiting": w, "tier": ti}
    for c in (None, "billing")
    for u in (None, True, False)
    for ti in (None, "vip", "standard")
    for w in [None] + list(range(0, 61)) + [0.25, 0.5, 0.75]
]

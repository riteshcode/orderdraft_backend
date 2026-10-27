"""
List endpoints ki sorting:  ?sort=name  (A-Z)   ?sort=-price  (bada pehle)

    SORTS = {"name": Product.name, "price": Product.price}
    stmt = apply_sort(stmt, sort, SORTS, default=Product.name, tiebreak=Product.id)

Sirf whitelist wale columns - koi bhi random column / SQL nahi chalega.
Galat ya khali sort = default order.
"""


def apply_sort(stmt, sort: str | None, allowed: dict, default, tiebreak=None, default_desc: bool = False):
    sort = (sort or "").strip()
    desc = sort.startswith("-")
    column = allowed.get(sort.lstrip("-"))
    if column is None:                       # galat / khali -> default
        column, desc = default, default_desc
    order = [column.desc() if desc else column.asc()]
    if tiebreak is not None:                 # same value wale rows ka order fix (pagination stable)
        order.append(tiebreak.desc() if desc else tiebreak.asc())
    return stmt.order_by(*order)

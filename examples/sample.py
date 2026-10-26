import sys

def seq(a):
    x = a + 1
    return x * 2

def branches(n):
    if n < 0:
        return "neg"
    elif n == 0:
        return "zero"
    else:
        r = "pos"
    print("unreachable?") if n else None
    return r

def loops(items):
    total = 0
    for i in items:
        if i is None:
            continue
        if i > 100:
            break
        total += i
    else:
        print("no break")
    while total > 10 and total % 2 == 0 or total == 3:
        total //= 2
    else:
        pass
    return total

def dead(x):
    return x
    print("dead")
    if x:
        y = 1

def exc(path):
    try:
        f = open(path)
        data = f.read()
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise RuntimeError(e)
    else:
        print("ok")
    finally:
        print("done")
    with open(path) as g, open(path) as h:
        g.read()
    return data

def matcher(cmd):
    match cmd:
        case ["go", d] if d in ("n", "s") or d == "e":
            return 1
        case {"x": 1}:
            return 2
        case 1 | 2:
            return 3
        case _:
            return 4

async def aio(xs):
    async for x in xs:
        async with x:
            y = x and xs or None
    return [v for v in xs if v or not v]

class C:
    def method(self):
        def inner(z):
            return lambda q: q or z
        while True:
            if self:
                break
        return inner

if __name__ == "__main__":
    print(C().method())

def fin_return(path):
    try:
        return open(path).read()
    finally:
        print("closing")

def fin_loop(items):
    for x in items:
        try:
            if x is None:
                continue
            if x < 0:
                break
            if x == 0:
                return "zero"
        finally:
            if x:
                print("cleanup", x)
    return "done"

def fin_nested(a):
    try:
        try:
            return a
        finally:
            print("inner")
    finally:
        print("outer")

def exprs(xs, flag):
    y = 1 if flag else 2
    pairs = [(a, b) for a in xs if a for b in xs if b > a]
    lookup = {k: (v if v else 0) for k, v in xs}
    assert y > 0, "positive"
    return y, pairs, lookup

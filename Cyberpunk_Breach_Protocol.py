import os
import sys
from itertools import permutations
from typing import List, Tuple, Optional

Cell = Tuple[int, int]

def contains_subsequence(haystack: List[str], needle: List[str]) -> bool:
    n, m = len(haystack), len(needle)
    if m == 0:
        return True
    if m > n:
        return False
    for i in range(n - m + 1):
        if haystack[i:i + m] == needle:
            return True
    return False

class BreachSolver:
    def __init__(self, matrix: List[List[str]], ram: int) -> None:
        self.matrix = matrix
        self.rows = len(matrix)
        self.cols = len(matrix[0]) if self.rows else 0
        self.ram = ram

    def solve(self, targets: List[List[str]]) -> Optional[List[Cell]]:
        best: List[Cell] = []
        matrix = self.matrix
        rows, cols = self.rows, self.cols
        
        def dfs(r: int, c: int, vertical: bool,
                path: List[Cell], visited: set, path_vals: List[str]) -> None:
            nonlocal best
            if best and len(path) >= len(best):
                return
            if all(contains_subsequence(path_vals, seq) for seq in targets):
                best = list(path)
                return
            if vertical:
                for nr in range(rows):
                    if nr == r or (nr, c) in visited:
                        continue
                    visited.add((nr, c))
                    path.append((nr, c))
                    path_vals.append(matrix[nr][c])
                    dfs(nr, c, False, path, visited, path_vals)
                    path_vals.pop()
                    path.pop()
                    visited.discard((nr, c))
            else:
                for nc in range(cols):
                    if nc == c or (r, nc) in visited:
                        continue
                    visited.add((r, nc))
                    path.append((r, nc))
                    path_vals.append(matrix[r][nc])
                    dfs(r, nc, True, path, visited, path_vals)
                    path_vals.pop()
                    path.pop()
                    visited.discard((r, nc))

        for c in range(cols):
            start = (0, c)
            dfs(0, c, True, [start], {start}, [matrix[0][c]])

        return best or None

def enumerate_target_combinations(daemons: List[List[str]]) -> List[List[List[str]]]:
    seen = set()
    ordered: List[List[List[str]]] = []
    for k in range(len(daemons), 0, -1):
        for perm in permutations(daemons, k):
            key = tuple(tuple(s) for s in perm)
            if key in seen:
                continue
            seen.add(key)
            ordered.append([list(s) for s in perm])
    return ordered

def breach(hexdump: List[List[str]], ram: int,
           d1: List[str], d2: List[str], d3: List[str]):
    daemons = [d1, d2, d3]
    solver = BreachSolver(hexdump, ram)
    for targets in enumerate_target_combinations(daemons):
        path = solver.solve(targets)
        if path:
            return targets, path
    return None, None

class MatrixRenderer:
    PALETTE = [
        "\033[1;101;30m", "\033[1;103;30m", "\033[1;102;30m", "\033[1;106;30m",
        "\033[1;104;30m", "\033[1;105;30m", "\033[1;43;30m",  "\033[1;107;30m",
    ]
    RESET = "\033[0m"
    DIM = "\033[2;37m"
    ARROW = "\033[1;90m->\033[0m"
    @classmethod
    def _enable_ansi(cls) -> None:
        if sys.platform.startswith("win"):
            os.system("")
    @classmethod
    def render(cls, matrix: List[List[str]], path: List[Cell]) -> None:
        cls._enable_ansi()
        step_index = {coord: idx for idx, coord in enumerate(path)}
        divider = "=" * 50
        print()
        print(divider)
        print("    MA TRẬN BREACH PROTOCOL")
        print(divider)
        print()
        for r in range(len(matrix)):
            cells = []
            for c in range(len(matrix[0])):
                val = matrix[r][c]
                if (r, c) in step_index:
                    idx = step_index[(r, c)]
                    color = cls.PALETTE[idx % len(cls.PALETTE)]
                    cells.append(f"{color} {val:^4} {cls.RESET}")
                else:
                    cells.append(f"{cls.DIM} {val:^4} {cls.RESET}")
            print(" ".join(cells))
        print()
        print(divider)
        print("    LUỒNG ĐI (Thứ tự từ Trái -> Phải)")
        print(divider)
        print()
        chunks = []
        for i, (r, c) in enumerate(path):
            val = matrix[r][c]
            color = cls.PALETTE[i % len(cls.PALETTE)]
            chunks.append(f"{color} {val} {cls.RESET}")
            if i < len(path) - 1:
                chunks.append(cls.ARROW)
        print("  " + " ".join(chunks))
        print()

if __name__ == "__main__":
    hexdump = [
        ['1C', 'E9', 'BD', '55', '1C'],
        ['E9', '1C', 'E9', 'BD', '55'],
        ['55', 'FF', '7A', 'FF', '1C'],
        ['1C', 'BD', 'E9', '1C', 'BD'],
        ['55', '1C', 'BD', '1C', '1C'],
    ]
    ram_limit = 8
    datamine_a = ['1C', '55']
    datamine_b = ['BD', '7A', '55']
    datamine_c = ['55', '1C', '1C', 'BD']
    resolved, traversal = breach(hexdump, ram_limit, datamine_a, datamine_b, datamine_c)
    if traversal:
        labels = ", ".join("[" + " ".join(seq) + "]" for seq in resolved)
        print(f"\nĐÃ GIẢI CÁC DAEMON: {labels}")
        MatrixRenderer.render(hexdump, traversal)
    else:
        print("\nKhông tìm thấy đường đi hợp lệ.")
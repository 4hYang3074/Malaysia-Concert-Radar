"""在你自己的电脑上执行（马来西亚 IP）：抓 GitHub 机房会被挡的平台，存成 state/local/<平台>.json。
GitHub 上的扫描抓不到这些平台时，就改用这里的资料。只用 Python 标准库。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan  # noqa: E402

SOURCES = {"BookMyShow": scan.bookmyshow}


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 终端默认编码印不出中文
    out_dir = scan.ROOT / "state" / "local"
    out_dir.mkdir(parents=True, exist_ok=True)
    h = scan.Http()
    for name, fn in SOURCES.items():
        path = out_dir / f"{name.lower()}.json"
        try:
            events = fn(h)
        except Exception as e:
            print(f"{name}: 失败 {e}")
            continue
        if not events:
            print(f"{name}: 没有资料，不更新")
            continue
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if old.get("events") == events:  # 内容没变就不改档，避免多余的提交
            print(f"{name}: 没有变化")
            continue
        path.write_text(json.dumps({"fetched_at": scan.now_myt().strftime("%Y-%m-%d %H:%M"), "events": events},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{name}: {len(events)} 场，已更新")


if __name__ == "__main__":
    main()

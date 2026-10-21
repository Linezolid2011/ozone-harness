"""原子写：同目录临时文件 + `os.replace`。

**为什么单独立一个模块**：`data/` 下那几个 JSON（接口档案、联网工具设置、界面语言、
记忆上限）早先都是"整个文件 `write_text`"。半截写入的后果不是"少一行"，
而是**文件读不出来** —— 而它们的读端为了容错会把"读不出来"当成"没有配置"：
`providers.json` 坏掉的表现就是"所有接口和 key 都不见了"，而且没有任何提示，
下一次保存还会把坏文件覆盖掉（原来的内容再也回不来）。

vault / prompts / dream 运行记录早就用"临时文件 + `os.replace`"了；这个模块把那套
做法收口成一处，让上面四个 store 也用同一套 —— 一次 `write_text` 被 Ctrl+C、
断电或另一个线程同时写截断，都不该产生一个读不出来的文件。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path


def write_text_atomic(path: Path, text: str) -> None:
    """把 `text` 原子地写到 `path`。

    临时文件放在**同一个目录**里，所以 `os.replace` 是同卷内的原子改名；
    `fsync` 是为了让"写完了"这件事真的落到盘上（否则断电后可能只剩一个空文件）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:6]}")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        # 成功时临时文件已经被换走了；失败时把它清掉，别在 data/ 里留垃圾
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass

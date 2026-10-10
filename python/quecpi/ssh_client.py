"""SSH 客户端封装 (paramiko): 命令执行 + SFTP 文件传输。

- 懒连接: 首次使用时连接, 断线自动重连
- 流式输出: exec_command 按行 yield
"""
from __future__ import annotations

import codecs
import os
import threading
import time
from typing import List, Optional, Tuple


class SSHClient:
    def __init__(self, host: str, port: int, user: str,
                 password: Optional[str] = None,
                 key_path: Optional[str] = None):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.key_path = key_path
        self._client = None
        self._lock = threading.Lock()

    # ---------- 连接管理 ----------
    def connect(self) -> None:
        import paramiko
        with self._lock:
            if self._client is not None and self._client.get_transport() is not None \
                    and self._client.get_transport().is_active():
                return
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kwargs: dict = dict(hostname=self.host, port=self.port,
                                username=self.user, timeout=10)
            if self.key_path and os.path.exists(os.path.expanduser(self.key_path)):
                kwargs["key_filename"] = os.path.expanduser(self.key_path)
            if self.password:
                kwargs["password"] = self.password
            client.connect(**kwargs)
            self._client = client

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

    def ensure(self):
        self.connect()
        assert self._client is not None
        return self._client

    # ---------- 命令执行 ----------
    def run(self, cmd: str, cwd: str = "", env: Optional[dict] = None,
            timeout: Optional[float] = None) -> Tuple[int, str, str]:
        """同步执行, 返回 (exit_code, stdout, stderr)。用于短命令。"""
        full = self._build_cmd(cmd, cwd, env)
        client = self.ensure()
        stdin, stdout, stderr = client.exec_command(full, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err

    def stream(self, cmd: str, cwd: str = "", env: Optional[dict] = None,
               line_cb=None, stop_event: Optional[threading.Event] = None,
               input_data: Optional[str] = None,
               raw: bool = False) -> Tuple[int, List[str]]:
        """流式执行: 逐行回调 line_cb(line) 或收集到列表。stop_event 置位时主动关闭 channel 中断远端。

        input_data 非空时写入 channel stdin 并关闭写通道 (用于 sudo -S 等从 stdin 读输入的场景)。
        raw=True 时不拆行, 按块原样回调 (含 \\r 与 ANSI 转义), 交给前端终端模拟器
        处理 —— 用于 pty 下的交互式输出 (进度条靠 \\r 重绘 + 光标上移), 拆行会破坏
        光标语义。用增量解码器跨块保留不完整的多字节字符。
        """
        full = self._build_cmd(cmd, cwd, env)
        client = self.ensure()
        lines: List[str] = []
        stdin, stdout, stderr = client.exec_command(full, get_pty=False)
        if input_data is not None:
            try:
                stdin.write(input_data)
                stdin.channel.shutdown_write()
            except Exception:
                pass
        chan = stdout.channel
    
        chan.settimeout(0.2)

        # raw 模式下按块透传, 用增量解码器跨块拼接不完整的多字节字符
        dec = {"out": codecs.getincrementaldecoder("utf-8")("replace"),
               "err": codecs.getincrementaldecoder("utf-8")("replace")}

        def _recv(fn):
            try:
                return fn(8192)
            except OSError:
                return b""

        def _emit(buffers: dict, data: bytes, key: str):
            if raw:
                text = dec[key].decode(data)
                if text:
                    lines.append(text)
                    if line_cb:
                        line_cb(text)
                return
            buffers[key] += data
            # 同时按 \n 和 \r 断行 (\r\n 视为一个分隔符): curl 下载进度用 \r 原地
            # 刷新、不产生 \n, 若不按 \r 断行则整个长下载期间不会产出任何一行,
            # 前端 SSE 会因长时间无数据被 undici bodyTimeout 掐断。
            buf = buffers[key]
            while True:
                nl = buf.find(b"\n")
                cr = buf.find(b"\r")
                if nl < 0 and cr < 0:
                    break
                if cr >= 0 and (nl < 0 or cr < nl):
                    idx, sep = cr, 1
                    if cr + 1 < len(buf) and buf[cr + 1:cr + 2] == b"\n":
                        sep = 2
                else:
                    idx, sep = nl, 1
                line = buf[:idx]
                buf = buf[idx + sep:]
                text = line.decode("utf-8", errors="replace")
                lines.append(text)
                if line_cb:
                    line_cb(text)
            buffers[key] = buf

        def _flush(buffers: dict):
            for key in ("out", "err"):
                if raw:
                    text = dec[key].decode(b"", final=True)
                elif buffers[key]:
                    text = buffers[key].decode("utf-8", errors="replace")
                    buffers[key] = b""
                else:
                    text = ""
                if text:
                    lines.append(text)
                    if line_cb:
                        line_cb(text)

        buf = {"out": b"", "err": b""}
        while True:
            if stop_event is not None and stop_event.is_set():
                try:
                    chan.close()
                except Exception:
                    pass
                break
            out_chunk = _recv(chan.recv)
            err_chunk = _recv(chan.recv_stderr)
            if out_chunk:
                _emit(buf, out_chunk, "out")
            if err_chunk:
                _emit(buf, err_chunk, "err")
            if not out_chunk and not err_chunk:
                if chan.exit_status_ready():
                    # 退出前排干两个通道剩余数据
                    while True:
                        o = _recv(chan.recv)
                        e = _recv(chan.recv_stderr)
                        if o:
                            _emit(buf, o, "out")
                        if e:
                            _emit(buf, e, "err")
                        if not o and not e:
                            break
                    break
                time.sleep(0.1)
        _flush(buf)
        code = chan.recv_exit_status()
        return code, lines

    def _build_cmd(self, cmd: str, cwd: str, env: Optional[dict]) -> str:
        parts: List[str] = []
        if cwd:
            parts.append(f"cd {self._quote(cwd)}")
        if env:
            for k, v in env.items():
                parts.append(f"export {k}={self._quote(str(v))}")
        parts.append(cmd)
        return " && ".join(parts)

    @staticmethod
    def _quote(s: str) -> str:
        return "'" + s.replace("'", "'\\''") + "'"

    # ---------- SFTP ----------
    def read_file(self, remote_path: str) -> str:
        sftp = self.ensure().open_sftp()
        try:
            with sftp.open(remote_path, "r") as f:
                return f.read().decode("utf-8", errors="replace")
        finally:
            sftp.close()

    def list_dir(self, remote_path: str) -> List[dict]:
        sftp = self.ensure().open_sftp()
        try:
            out = []
            for attr in sftp.listdir_attr(remote_path):
                out.append({
                    "name": attr.filename,
                    "size": attr.st_size,
                    "mtime": attr.st_mtime,
                    "isdir": bool(attr.st_mode & 0o040000),
                })
            return out
        finally:
            sftp.close()

    def pull(self, remote_path: str, local_path: str) -> None:
        sftp = self.ensure().open_sftp()
        try:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            sftp.get(remote_path, local_path)
        finally:
            sftp.close()

    def push(self, local_path: str, remote_path: str) -> None:
        sftp = self.ensure().open_sftp()
        try:
            sftp.put(local_path, remote_path)
        finally:
            sftp.close()

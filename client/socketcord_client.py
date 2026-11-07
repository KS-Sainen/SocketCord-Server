"""Reference SocketCord client: standard library only, one TCP socket, one reader thread.

Two uses:

1. A terminal client for testing the backend without the GUI:

       python3 client/socketcord_client.py --host 127.0.0.1 --port 8080

2. A worked example for the GUI. It sends exactly what the GUI has to send,
   including the password in clear text on register, login and password change
   (wire contract v2; the socket is not encrypted). Its read loop mirrors
   network.py: packets go into a buffer, complete ones are parsed, a packet
   with no "method" is a server push, everything else is a reply matched to
   its request by the "rid" the client put in the request.
"""
from __future__ import annotations

import argparse
import getpass
import json
import shlex
import socket
import sys
import threading
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from socketcord import scram  # noqa: E402
from socketcord.protocol import GET, NIL_UUID, PATCH, POST, ReplyCord, ReplyParser, RequestCord  # noqa: E402


AUTO_DEVICE = object()   # register(): send scram.device_fingerprint()


class ApiFailure(Exception):
    def __init__(self, reply: ReplyCord, note: str = "") -> None:
        self.reply = reply
        self.status = reply.status
        self.data = reply.data
        super().__init__(note or f"{reply.status} {reply.data.get('error')}: {reply.data.get('detail')}")


class SocketCordClient:
    def __init__(self, host: str, port: int, timeout: float = 10.0,
                 on_event: Optional[Callable[[ReplyCord], None]] = None) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(None)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.timeout = timeout
        self.on_event = on_event
        self.user_id = NIL_UUID
        self.token = NIL_UUID
        self.username: Optional[str] = None
        self._cond = threading.Condition()
        self._replies: Dict[object, ReplyCord] = {}
        self._unmatched: List[ReplyCord] = []
        self._events: List[ReplyCord] = []
        self._closed = False
        self._rid = 0
        self._send_lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, name="socketcord-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    def __enter__(self) -> "SocketCordClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def send_raw(self, data: bytes) -> None:
        with self._send_lock:
            self.sock.sendall(data)

    def _read_loop(self) -> None:
        parser = ReplyParser()
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    break
                for item in parser.feed(data):
                    if isinstance(item, ValueError):
                        continue
                    if "method" not in item.data:      # a push; see protocol.PUSH_PATHS
                        with self._cond:
                            self._events.append(item)
                            self._cond.notify_all()
                        if self.on_event:
                            self.on_event(item)
                        continue
                    with self._cond:
                        rid = item.data.get("rid")
                        if rid is None:
                            self._unmatched.append(item)
                        else:
                            self._replies[rid] = item
                        self._cond.notify_all()
        except OSError:
            pass
        finally:
            with self._cond:
                self._closed = True
                self._cond.notify_all()

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    def next_rid(self) -> int:
        with self._cond:
            self._rid += 1
            return self._rid

    def wait_reply(self, rid: object, timeout: Optional[float] = None) -> ReplyCord:
        with self._cond:
            self._cond.wait_for(lambda: rid in self._replies or self._closed, timeout or self.timeout)
            if rid in self._replies:
                return self._replies.pop(rid)
        raise ConnectionError("connection closed") if self.closed else TimeoutError(f"no reply for rid {rid}")

    def wait_unmatched(self, timeout: Optional[float] = None) -> ReplyCord:
        """For replies that carry no rid (e.g. a corrupted packet the server could not read)."""
        with self._cond:
            self._cond.wait_for(lambda: self._unmatched or self._closed, timeout or self.timeout)
            if self._unmatched:
                return self._unmatched.pop(0)
        raise TimeoutError("no reply")

    def wait_event(self, path: str, timeout: float = 5.0,
                   match: Optional[Callable[[dict], bool]] = None) -> ReplyCord:
        """The next push with this `path` (one of protocol.PUSH_PATHS)."""

        def find() -> Optional[int]:
            for i, ev in enumerate(self._events):
                if ev.path == path and (match is None or match(ev.data)):
                    return i
            return None

        with self._cond:
            self._cond.wait_for(lambda: find() is not None or self._closed, timeout)
            idx = find()
            if idx is None:
                raise TimeoutError(f"no {path} within {timeout}s")
            return self._events.pop(idx)

    def drain_events(self) -> List[ReplyCord]:
        with self._cond:
            out, self._events = self._events, []
            return out

    def request(self, method: int, url: str, data: Optional[dict] = None, auth: bool = True,
                timeout: Optional[float] = None) -> ReplyCord:
        rid = self.next_rid()
        msg = dict(data or {})
        msg["rid"] = rid
        req = RequestCord.from_dict(method, url, msg,
                                    user_id=self.user_id if auth else NIL_UUID,
                                    ses_token=self.token if auth else NIL_UUID)
        self.send_raw(req.pack())
        return self.wait_reply(rid, timeout)

    def call(self, method: int, url: str, data: Optional[dict] = None, auth: bool = True,
             timeout: Optional[float] = None) -> dict:
        """Like request(), but raises ApiFailure unless the status is 200."""
        reply = self.request(method, url, data, auth, timeout)
        if not reply.ok:
            raise ApiFailure(reply)
        return reply.data

    def get(self, url: str, data: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
        return self.call(GET, url, data, timeout=timeout)

    def post(self, url: str, data: Optional[dict] = None) -> dict:
        return self.call(POST, url, data)

    def patch(self, url: str, data: Optional[dict] = None) -> dict:
        return self.call(PATCH, url, data)

    # The password travels in the message body as clear text, on an unencrypted
    # socket (contract v2 section 1). The server hashes it and stores only the hash.
    def register(self, username: str, password: str, device_id=AUTO_DEVICE) -> dict:
        """device_id defaults to a hash of this machine's MAC; pass None to send none."""
        msg = {"username": username, "password": password}
        if device_id is AUTO_DEVICE:
            device_id = scram.device_fingerprint()
        if device_id:
            msg["device_id"] = device_id
        return self.call(POST, "users", msg, auth=False)

    def login(self, username: str, password: str) -> dict:
        data = self.call(POST, "users/login", {"username": username, "password": password}, auth=False)
        self.user_id = uuid.UUID(data["user_id"])
        self.token = uuid.UUID(data["session_token"])
        self.username = data["username"]
        return data

    def change_password(self, new_password: str) -> dict:
        """No current password is asked for, and the session token does not change."""
        return self.patch("users/password", {"new_password": new_password})

    def logout(self) -> dict:
        data = self.post("users/logout")
        self.user_id, self.token, self.username = NIL_UUID, NIL_UUID, None
        return data

    # Claude needs a few seconds (the server allows up to about 2 x AI_TIMEOUT_SECONDS),
    # so this call waits longer than the others.
    SUMMARY_TIMEOUT = 90.0

    def summary(self, chat_id, mode: str = "recent", count: int = 50, language: str = "auto",
                timeout: float = SUMMARY_TIMEOUT) -> dict:
        """mode "recent": the latest `count` messages; "unread": the unread ones among them."""
        return self.get(f"chat/{chat_id}/summary", {"mode": mode, "count": count, "language": language},
                        timeout=timeout)


HELP = """commands:
  register <name> [password]      login <name> [password]       logout      passwd
  online                          chats                          groups      requests
  create <group name...>          join <chat_id>                 leave <chat_id>
  info <chat_id>                  history <chat_id> [count]      read <chat_id>
  summary <chat_id> [recent|unread] [count] [language]     (AI summary of the chat)
  send <chat_id> <text...>        dm <name|user_id> <text...>    invite <chat_id> <name|user_id>
  accept <request_id>             decline <request_id>           cancel <request_id>
  profile <#text> [#background]   raw <GET|POST|PATCH> <url> [json]
  search [name prefix]
  help                            quit"""


def _print_event(ev: ReplyCord) -> None:
    d = dict(ev.data)
    name = d.pop("path", "")
    if name == "chat/new_message":
        m = d["message"]
        print(f"\n[{d['chat_id'][:8]}] {m['sender'] or '*'}: {m['text']}")
    elif name == "users/status":
        print(f"\n* {d['user']['username']} is {'online' if d['online'] else 'offline'}")
    elif name == "users/request":
        r = d["request"]
        what = f"DM: {r.get('message')!r}" if r["kind"] == "dm" else f"invite to {r['chat']['name']}"
        print(f"\n* request {r['request_id']} from {r['from']['username']} ({what}) is {r['status']}")
    else:
        print(f"\n* {name}: {json.dumps(d, ensure_ascii=False)}")
    print("> ", end="", flush=True)


def _resolve_user(client: SocketCordClient, who: str) -> str:
    try:
        return str(uuid.UUID(who))
    except ValueError:
        pass
    for u in client.get("users/search", {"query": who, "count": 1})["users"]:
        if u["username"].lower() == who.lower():     # an exact match comes first
            return u["user_id"]
    raise ValueError(f"cannot find user {who!r}")


def _rest(line: str, skip: int) -> str:
    """The text after the first `skip` words, exactly as typed (quotes and all)."""
    parts = line.split(None, skip)
    return parts[skip] if len(parts) > skip else ""


def _show(data: dict) -> None:
    print(json.dumps({k: v for k, v in data.items() if k not in ("path", "rid")}, ensure_ascii=False, indent=2))


def repl(host: str, port: int) -> None:
    client = SocketCordClient(host, port, on_event=_print_event)
    print(f"connected to {host}:{port}. type 'help'.")
    methods = {"GET": GET, "POST": POST, "PATCH": PATCH}
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        try:
            try:
                cmd, *args = shlex.split(line)
            except ValueError:          # e.g. an apostrophe in "send <id> it's working"
                cmd, *args = line.split()
            if cmd in ("quit", "exit"):
                break
            elif cmd == "help":
                print(HELP)
            elif cmd in ("register", "login"):
                password = args[1] if len(args) > 1 else getpass.getpass("password: ")
                if cmd == "register":
                    _show(client.register(args[0], password))
                else:
                    data = client.login(args[0], password)
                    print(f"logged in as {data['username']} ({data['user_id']})")
            elif cmd == "logout":
                _show(client.logout())
            elif cmd == "passwd":
                _show(client.change_password(getpass.getpass("new password: ")))
            elif cmd == "online":
                for u in client.get("users/online")["users"]:
                    print(f"  {u['username']}{' (you)' if u['is_self'] else ''}  {u['user_id']}")
            elif cmd == "chats":
                for c in client.get("chats")["chats"]:
                    print(f"  {c['chat_id']}  [{c['kind']}] {c['name']}  unread={c['unread']}")
            elif cmd == "groups":
                for g in client.get("chats/groups")["groups"]:
                    names = ", ".join(m["username"] for m in g["members"])
                    print(f"  {g['chat_id']}  {g['name']}{' (joined)' if g['joined'] else ''}: {names}")
            elif cmd == "requests":
                _show(client.get("users/requests"))
            elif cmd == "create":
                _show(client.post("chats", {"name": " ".join(args)}))
            elif cmd in ("join", "leave"):
                _show(client.post(f"chat/{args[0]}/{cmd}"))
            elif cmd == "info":
                _show(client.get(f"chat/{args[0]}/info"))
            elif cmd == "history":
                count = int(args[1]) if len(args) > 1 else 20
                for m in client.get(f"chat/{args[0]}/messages", {"count": count})["messages"]:
                    print(f"  {m['time']}  {m['sender'] or '*'}: {m['text']}")
            elif cmd == "read":
                _show(client.post(f"chat/{args[0]}/read"))
            elif cmd == "summary":
                mode = args[1] if len(args) > 1 else "recent"
                count = int(args[2]) if len(args) > 2 else 50
                language = args[3] if len(args) > 3 else "auto"
                print("asking for a summary (this can take a few seconds)...")
                s = client.summary(args[0], mode, count, language)
                if s["summary"] is None:
                    print("  nothing to summarize")
                else:
                    print(s["summary"])
                    print(f"  ({s['message_count']} messages, engine={s['engine']}, model={s['model']}, "
                          f"cached={s['cached']})")
            elif cmd == "send":
                _show(client.post(f"chat/{args[0]}/message", {"content": _rest(line, 2)}))
            elif cmd == "dm":
                _show(client.post(f"users/{_resolve_user(client, args[0])}/dm", {"message": _rest(line, 2)}))
            elif cmd == "invite":
                _show(client.post(f"chat/{args[0]}/invite", {"user_id": _resolve_user(client, args[1])}))
            elif cmd in ("accept", "decline", "cancel"):
                _show(client.post(f"users/requests/{args[0]}/{cmd}"))
            elif cmd == "profile":
                colours = {"text_color": args[0]}
                if len(args) > 1:
                    colours["background"] = args[1]
                _show(client.patch("users/profile", colours))
            elif cmd == "search":
                for u in client.get("users/search", {"query": args[0] if args else ""})["users"]:
                    print(f"  {u['username']}{' (you)' if u['is_self'] else ''}  {u['user_id']}  "
                          f"{u['text_color']} on {u['background']}{'  online' if u['online'] else ''}")
            elif cmd == "raw":
                if args[0].upper() not in methods:
                    raise ValueError("method must be GET, POST or PATCH")
                body = json.loads(_rest(line, 3)) if len(args) > 2 else None
                if body is not None and not isinstance(body, dict):
                    raise ValueError("the message must be a JSON object")
                reply = client.request(methods[args[0].upper()], args[1], body)
                print(reply.status)
                _show(reply.data)
            else:
                print("unknown command; type 'help'")
        except ApiFailure as exc:
            print(f"error {exc.status}: {exc.data.get('error')} - {exc.data.get('detail')}")
        except (IndexError, ValueError, KeyError, TypeError) as exc:
            print(f"usage error: {exc}")
        except (ConnectionError, TimeoutError) as exc:
            print(f"connection problem: {exc}")
            if client.closed:
                break
    client.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SocketCord terminal client")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ns = ap.parse_args()
    repl(ns.host, ns.port)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TS3 ServerQuery 事件 -> Bark 推送   （仅标准库，无依赖）

用法:
  python3 ts3_bark.py --probe    干跑：只打印收到的事件，不推送
  python3 ts3_bark.py --who      诊断：列出服务器上所有客户端（含 Query/僵尸连接）
  python3 ts3_bark.py            正式运行

设计要点（治「长时间没人就漏报」）:
  1. 常驻长连接 + 每 IDLE_SEC 秒发一次 version 保活，避免 Query 空闲被踢
  2. 断线自动重连，并且重连后重新 login / use / servernotifyregister
     （事件订阅是「连接级」的，连接一断订阅就没了，必须重新注册）
  3. 每 IDLE_SEC 秒 clientlist 对账兜底，事件真的漏了最多 IDLE_SEC 秒后补报
  4. 昵称冲突(error id=513)自动换名；退出前先 quit 释放客户端再关连接；
     重连带指数退避（5→10→20→30→60 秒），不会疯狂冲击服务端
"""
import json, os, random, re, socket, sys, time, urllib.request

PROBE = "--probe" in sys.argv

# ---------------- TS3 ----------------
# TS3_HOST / TS3_USER / TS3_PASS 是必填项，故意不给默认值：
# 请在 docker run 时用 -e 传入（环境变量永远优先于本文件里的默认值）。
TS3_HOST  = os.getenv("TS3_HOST", "")
TS3_QPORT = int(os.getenv("TS3_QPORT", "10011"))
TS3_PORT  = int(os.getenv("TS3_PORT", "9987"))
TS3_USER  = os.getenv("TS3_USER", "")
TS3_PASS  = os.getenv("TS3_PASS", "")
BOT_NICK  = os.getenv("BOT_NICK", "BarkNotify")
# 默认不带跳转链接：留空 = 推送载荷里完全不发 url 字段，通知卡片更干净。
# 想要「点通知直接进 TS3 客户端」，传 -e TS3_LINK='ts3server://你的地址?port=9987'
TS3_LINK  = os.getenv("TS3_LINK", "")

# ---------------- Bark ----------------
# 官方服务: https://api.day.app
# 自建服务: http://<Bark 主机>:<端口>（内网脚本优先用内网地址，可避开 NAT 回环问题）
BARK_SERVER = os.getenv("BARK_SERVER", "").rstrip("/")   # 必填
BARK_KEY    = os.getenv("BARK_KEY", "")                  # 必填，设备 key（每台设备一个）
BARK_GROUP  = os.getenv("BARK_GROUP", "TeamSpeak")
BARK_SOUND  = os.getenv("BARK_SOUND", "calypso")

# ---------------- 策略 ----------------
WATCH_UIDS    = set(filter(None, os.getenv("WATCH_UIDS", "").split(",")))     # 空=所有人
MUTE_CHANNELS = set(filter(None, os.getenv("MUTE_CHANNELS", "").split(",")))  # 静音频道 cid
IDLE_SEC      = int(os.getenv("IDLE_SEC", "90"))     # 保活 + 对账节奏
DEDUP_SEC     = int(os.getenv("DEDUP_SEC", "60"))    # 同一人同动作去重窗口
REPORT_LEAVE  = os.getenv("REPORT_LEAVE", "1") == "1"

# 断线告警：连不上 TS3 时主动推 Bark（默认开），连续失败 ALERT_AFTER_FAILS 次才报一次；
# 恢复后再报一次「✅ TeamSpeak 恢复」（状态变化去重，不会刷屏）
ALERT_ON_ERROR    = os.getenv("ALERT_ON_ERROR", "1") == "1"
ALERT_AFTER_FAILS = int(os.getenv("ALERT_AFTER_FAILS", "3"))

def log(*a):
    print(time.strftime("[%m-%d %H:%M:%S]"), *a, flush=True)

def check_config():
    """必填项检查：缺参数时给一句人话，而不是抛一堆看不懂的异常"""
    required = [("TS3_HOST", TS3_HOST), ("TS3_USER", TS3_USER), ("TS3_PASS", TS3_PASS)]
    if not (PROBE or "--who" in sys.argv):   # 干跑/诊断模式不推送，不需要 Bark 参数
        required += [("BARK_SERVER", BARK_SERVER), ("BARK_KEY", BARK_KEY)]
    missing = [n for n, v in required if not v]
    if not missing:
        return
    log("❌ 缺少必需的环境变量：" + "、".join(missing))
    log("   参考命令（把值换成你自己的）：")
    log("   docker run -d --name ts3-bark --restart=unless-stopped \\")
    log("     -e TS3_HOST=192.168.1.10 \\")
    log("     -e TS3_USER=你的ServerQuery账号 -e TS3_PASS=你的密码 \\")
    log("     -e BARK_SERVER=https://api.day.app -e BARK_KEY=你的设备key \\")
    log("     ts3-bark:latest")
    log("   只想先验证 TS3 能不能连上：加 --probe，此时只需 TS3_HOST / TS3_USER / TS3_PASS")
    sys.exit(1)

# ---------------- TS3 转义 ----------------
_ESC = {r"\\": "\\", r"\/": "/", r"\s": " ", r"\p": "|", r"\a": "\a", r"\b": "\b",
        r"\f": "\f", r"\n": "\n", r"\r": "\r", r"\t": "\t", r"\v": "\v"}
_ESC_RE = re.compile(r"\\[\\/spabfnrtv]")

def unescape(s):
    return _ESC_RE.sub(lambda m: _ESC.get(m.group(0), m.group(0)), s)

def escape(s):
    return (s.replace("\\", r"\\").replace("/", r"\/")
             .replace(" ", r"\s").replace("|", r"\p"))

def parse_kv(line):
    """按空格切分即可：TS3 协议里值中的空格一定被转义成 \\s，
    所以裸空格必然是字段分隔符。

    千万别只切「后面跟着 xxx= 的位置」——事件里有一批**有键无值**的字段
    （client_flag_avatar / client_meta_data / client_away_message /
    client_description / client_integrations ...），那样切会把裸键粘到
    前一个字段的值上，于是 client_type 变成 "0 client_flag_avatar"，
    结果每个真人都被当成机器人过滤掉（这就是之前收不到推送的原因）。
    """
    out = {}
    for it in line.split():
        if "=" in it:
            k, v = it.split("=", 1)
            out[k] = unescape(v)
    return out

def is_query_client(it):
    """只认 client_type：真人=0，ServerQuery/机器人=1。

    注意：绝对不能用 client_database_id 判断！实测某些 TS3 上，ServerQuery 登录
    账号的 dbid 和真人玩家一样（例如都是 3），拿它过滤会把真人的进服事件全部
    误杀。
    """
    return it.get("client_type", "0") != "0"

# ---------------- 推送 ----------------
def _bark_post(path, payload):
    req = urllib.request.Request(
        f"{BARK_SERVER}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST")
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status, r.read(200).decode("utf-8", "replace")

def bark(title, body):
    """干跑模式（--probe）只打印、绝不推送 —— 所以在干跑时收不到推送是正常的。

    正式模式先按 iOS App 文档的写法 POST /{key}；
    若该 bark-server 版本不接受（404 等），自动退回另一种写法
    POST /push（device_key 放进 JSON 里）。"""
    if PROBE or not BARK_SERVER or not BARK_KEY:
        log(f"[干跑/未配置] {title} -> {body}")
        return
    payload = {"title": title, "body": body, "group": BARK_GROUP,
               "sound": BARK_SOUND, "level": "active"}
    if TS3_LINK:
        payload["url"] = TS3_LINK
    try:
        st, txt = _bark_post(f"/{BARK_KEY}", payload)
        log("[bark]", st, txt)
    except Exception as e:
        log("[bark] POST /{key} 失败：", e, "→ 改用 /push 重试")
        try:
            p2 = dict(payload)
            p2["device_key"] = BARK_KEY
            st, txt = _bark_post("/push", p2)
            log("[bark]", st, txt)
        except Exception as e2:
            log("[bark] 推送失败:", e, "|", e2,
                "（检查 BARK_SERVER 是否可达 / DNS 回环）")

# ---------------- 状态 ----------------
ONLINE   = {}   # clid -> {nick, uid, cid}  当前在线（事件 + 对账共同维护）
CHANNELS = {}   # cid  -> 频道名
LAST     = {}   # uid  -> (时间戳, 动作)

def dedup(uid, action):
    ts, act = LAST.get(uid, (0, None))
    if act == action and time.time() - ts < DEDUP_SEC:
        return True
    LAST[uid] = (time.time(), action)
    return False

def chan_name(cid):
    return CHANNELS.get(cid, "未知频道")

def push_enter(nick, uid, cid, src=""):
    if MUTE_CHANNELS and cid in MUTE_CHANNELS:
        return
    if WATCH_UIDS and uid not in WATCH_UIDS:
        return
    if uid and dedup(uid, "enter"):
        return
    log(f"{src}进入: {nick} -> {chan_name(cid)}")
    bark("🎧 TeamSpeak 进入", f"{nick} 进入「{chan_name(cid)}」")

def push_leave(nick, cid, uid, src=""):
    if MUTE_CHANNELS and cid in MUTE_CHANNELS:
        return
    if WATCH_UIDS and uid and uid not in WATCH_UIDS:
        return
    if uid and dedup(uid, "leave"):
        return
    log(f"{src}离开: {nick} <- {chan_name(cid)}")
    bark("🚪 TeamSpeak 离开", f"{nick} 离开「{chan_name(cid)}」")

# ---------------- 事件分发 ----------------
def dispatch(line):
    if PROBE:
        log("<<", line)
    if line.startswith("notifycliententerview"):
        i = parse_kv(line.split(" ", 1)[1])
        if is_query_client(i) or i.get("client_nickname", "").startswith(BOT_NICK):
            if PROBE:
                log(f"   ↳ 已过滤（client_type={i.get('client_type')} "
                    f"dbid={i.get('client_database_id')} "
                    f"昵称={i.get('client_nickname')}）：Query/机器人自身")
            return
        clid = i.get("clid", "")
        c = {"nick": i.get("client_nickname", "?"),
             "uid": i.get("client_unique_identifier", ""),
             "cid": i.get("ctid", "")}
        ONLINE[clid] = c
        push_enter(c["nick"], c["uid"], c["cid"])
    elif line.startswith("notifyclientleftview"):
        i = parse_kv(line.split(" ", 1)[1])
        clid = i.get("clid", "")
        c = ONLINE.pop(clid, None)                  # 离开事件不带昵称，靠缓存反查
        if c and REPORT_LEAVE:
            push_leave(c["nick"], i.get("cfid", c["cid"]), c["uid"])

# ---------------- Query 通道 ----------------
class Query:
    def __init__(self, sock):
        self.sock, self.buf = sock, b""

    def read_line(self):
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line, self.buf = self.buf[:i], self.buf[i + 1:]
                return line.strip(b"\r").decode("utf-8", "replace")
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("对端关闭了 Query 连接")
            self.buf += chunk

    def send(self, cmd):
        self.sock.sendall((cmd + "\n").encode("utf-8"))

    def wait_ok(self, cmd):
        self.send(cmd)
        while True:
            line = self.read_line()
            if line.startswith("error id="):
                if "id=0" not in line:
                    raise RuntimeError(f"{cmd}  ->  {line}")
                return
            if line.startswith("notify"):
                dispatch(line)

    def collect(self, cmd):
        self.send(cmd)
        items, lines = [], []
        while True:
            line = self.read_line()
            if line.startswith("error id="):
                break
            if line.startswith("notify"):
                dispatch(line)
                continue
            if line:
                lines.append(line)
        for seg in "|".join(lines).split("|"):
            if "=" in seg:
                items.append(parse_kv(seg))
        return items

def load_channels(q):
    for it in q.collect("channellist"):
        if it.get("cid"):
            CHANNELS[it["cid"]] = it.get("channel_name", "?")

def set_nickname(q, base):
    """设置昵称；被占用(error id=513)时自动换一个带随机后缀的名字"""
    try:
        q.wait_ok(f"clientupdate client_nickname={escape(base)}")
        return base
    except RuntimeError as e:
        if "id=513" not in str(e):
            raise
        alt = f"{base}-{random.randrange(0x10000):04x}"
        log(f"昵称「{base}」已被占用（多半是上一次没释放的 Query 连接），改用「{alt}」")
        q.wait_ok(f"clientupdate client_nickname={escape(alt)}")
        return alt

def reconcile(q, first=False):
    """拉在线列表对账：补报被漏掉的进入/离开"""
    now = {}
    for it in q.collect("clientlist"):
        if "clid" not in it or is_query_client(it):
            continue
        now[it["clid"]] = {"nick": it.get("client_nickname", "?"),
                           "uid": it.get("client_unique_identifier", ""),
                           "cid": it.get("cid", "")}
    if first:
        ONLINE.update(now)
        log(f"基线建立：当前在线 {len(now)} 人，频道 {len(CHANNELS)} 个")
        return
    for clid, c in now.items():
        if clid not in ONLINE:
            push_enter(c["nick"], c["uid"], c["cid"], src="[对账补报]")
    for clid, c in list(ONLINE.items()):
        if clid not in now and REPORT_LEAVE:
            push_leave(c["nick"], c["cid"], c["uid"], src="[对账补报]")
    ONLINE.clear()
    ONLINE.update(now)

# ---------------- 会话 ----------------
def connect():
    sock = socket.create_connection((TS3_HOST, TS3_QPORT), timeout=15)
    q = Query(sock)
    q.wait_ok(f"login {escape(TS3_USER)} {escape(TS3_PASS)}")
    q.wait_ok(f"use port={TS3_PORT}")
    return sock, q

def session(sock, q, on_ready=None):
    nick = set_nickname(q, BOT_NICK)
    q.wait_ok("servernotifyregister event=server")      # 关键：订阅服务器级事件
    load_channels(q)
    reconcile(q, first=True)
    log(f"事件订阅成功（昵称 {nick}），等待进服事件…（Ctrl+C 退出）")
    if on_ready:
        on_ready()                                      # 告诉主循环：本次连接已就绪
    sock.settimeout(IDLE_SEC)
    while True:
        try:
            dispatch(q.read_line())
        except socket.timeout:
            q.wait_ok("version")                        # 保活：必须把响应读干净，
                                                        # 否则 version 的回复会污染
                                                        # 下一次 clientlist 的解析
            try:
                reconcile(q)                            # 兜底对账
            except Exception as e:
                log("对账失败:", e)

# ---------------- 诊断模式 ----------------
def who():
    sock, q = connect()
    load_channels(q)
    rows = [it for it in q.collect("clientlist") if "clid" in it]
    print("\n===== 服务器上的客户端 =====")
    for it in rows:
        kind = "Query" if is_query_client(it) else "真人 "
        print(f"{kind}  clid={str(it.get('clid','')):<8} 昵称={it.get('client_nickname','?')}"
              f"  频道={CHANNELS.get(it.get('cid',''), '?')}"
              f"  uid={it.get('client_unique_identifier','')}")
    nq = sum(1 for it in rows if is_query_client(it))
    print(f"\n共 {len(rows)} 个客户端，其中 Query/机器人 {nq} 个 —— "
          f"Query 数量明显偏多说明有僵尸连接没释放\n")
    try:
        sock.sendall(b"quit\n")
        time.sleep(0.2)
    except Exception:
        pass
    sock.close()

# ---------------- 主循环（先释放、再退避重连 + 断线告警） ----------------
def main():
    delay = 5
    fails = 0          # 连续失败次数
    alerted = False    # 是否已经报过「离线」
    down_at = 0        # 开始异常的时间

    def on_ready():
        """每次连接成功并且订阅完成后调用"""
        nonlocal fails, alerted, down_at
        fails = 0
        if alerted:
            dur = int(time.time() - down_at)
            log(f"TS3 连接已恢复（断开约 {dur} 秒）")
            if ALERT_ON_ERROR:
                bark("✅ TeamSpeak 恢复", f"连接已恢复，断开约 {dur} 秒")
            alerted = False
        down_at = 0

    while True:
        t0 = time.time()
        sock = None
        try:
            sock, q = connect()
            session(sock, q, on_ready)
        except Exception as e:
            fails += 1
            if fails == 1 and not alerted:
                down_at = time.time()
            if ALERT_ON_ERROR and not alerted and fails >= ALERT_AFTER_FAILS:
                alerted = True
                log(f"连续 {fails} 次连接失败，推送离线告警")
                bark("⛔ TeamSpeak 离线", f"连续 {fails} 次连接失败")
            if time.time() - t0 > 120:
                delay = 5                                # 稳定跑过 2 分钟，退避清零
            log(f"连接异常，{delay} 秒后重连:", e)
            time.sleep(delay)
            delay = min(delay * 2, 60)
        else:
            delay = 5
        finally:
            # 关键：每次断开前先 quit，让服务端立刻释放这个 Query 客户端，
            # 否则它可能还占着昵称，导致重连时撞上 error id=513
            if sock:
                try:
                    sock.sendall(b"quit\n")
                    time.sleep(0.2)
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass

if __name__ == "__main__":
    check_config()
    if "--who" in sys.argv:
        who()
    else:
        main()

import os
import json
import copy
import asyncio
import base64
import aiohttp
import websockets
from websockets.exceptions import ConnectionClosedError
from typing import Dict, Any, Optional, Tuple

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient
from solders.signature import Signature

from solders.pubkey import Pubkey
from solana.rpc.types import TokenAccountOpts
import struct

# ================= 配置 跟单钱包 和 个人钱包密钥 =================
API_KEY = "Your_Helius_API_Key"
SMART_WALLET = "Smart_Wallet_Address_To_Follow"   # 要跟单的钱包（领导）
FOLLOWER_SECRET = os.getenv("FOLLOWER_SECRET", "Your_Follower_Wallet_Private_Key")

RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={API_KEY}"
WSS_URL = f"wss://mainnet.helius-rpc.com/?api-key={API_KEY}"

# Jupiter 与 SOL 常量
SLIPPAGE_TOLERANCE = 0.128  # 20%（一级市场滑点给足以免卡单，自己把控）
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000

# HTTP 代理（如不需要可设为 None），这里请设置自己电脑的代理
PROXY = "your proxy"

# ========== 跟单参数（资金管理） ==========
FOLLOW_RATIO = 0.01                   # 跟随比例：我们花 = 领导花 * 该比例
MAX_PER_TRADE_SOL = 0.18             # 单笔最大花费 SOL
MIN_SOL_RESERVE = 0.02               # 至少保留这么多 SOL 不动
MIRROR_SELL = True                   # 是否跟单卖出（领导卖，我们也卖）
COOLDOWN_SEC = 6                     # 同一代币冷却，避免重复触发

# ================= 分批次出售 =================
SELL_STEPS = [
    0.25,  # 第一次 25% 总仓位
    0.40,  # 第二次 40% 剩余
    0.50,  # 第三次 50% 剩余
    0.50,  # 第四次 50% 剩余
    1.00   # 第五次 100% 剩余
]

# ================= 白名单 / 黑名单配置 =================
VIP_WALLETS = {
    "J6TDXvarvpBdPXTaTU8eJbtso1PUCYKGkVtMKUUY8iEa",
}
BLACKLIST_WALLETS = {
    "黑名单钱包地址1",
    "黑名单钱包地址2",
}
weighted_ratio = 2      # 加权系数，也就是增大持仓（2代币是前面计算出原定买入成本的2倍）

# ================= 初始化钱包（仅用 solders） =================
FOLLOWER_KEYPAIR = Keypair.from_base58_string(FOLLOWER_SECRET)
FOLLOWER_PUBKEY = str(FOLLOWER_KEYPAIR.pubkey())

# ================= 持仓与冷却（持久化） =================
POSITIONS_FILE = "positions.json"
_last_action_at: Dict[str, float] = {}   # mint -> timestamp

def now_ts() -> float:
    return asyncio.get_event_loop().time()

def load_positions() -> Dict[str, Any]:
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_positions(positions: Dict[str, Any]) -> None:
    tmp = POSITIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)
    os.replace(tmp, POSITIONS_FILE)

POSITIONS = load_positions()
# 结构：
# {
#   mint: {
#     "qty": int(基础单位数量),
#     "cost_lamports": int(总成本，lamports),
#     "last_sig": "xxxx"
#   },
#   ...
# }

# ================= 辅助：RPC 调用 =================
async def rpc_get_transaction(signature: str) -> Optional[Dict[str, Any]]:
    """getTransaction(signature, 'jsonParsed'), 支持 v0 交易。"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {
                "encoding": "jsonParsed",
                "commitment": "confirmed",
                "maxSupportedTransactionVersion": 0
            }
        ]
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(RPC_URL, json=payload, proxy=PROXY) as resp:
            data = await resp.json()
            return data.get("result")

async def rpc_get_balance(pubkey: str) -> int:
    """返回 lamports"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [pubkey, {"commitment": "confirmed"}],
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(RPC_URL, json=payload, proxy=PROXY) as resp:
            data = await resp.json()
            return int(data.get("result", {}).get("value", 0))

# ================= 分类：是否为领导“买入/卖出”交易 =================
def _account_keys_list(tx: Dict[str, Any]) -> list:
    ak = tx["transaction"]["message"]["accountKeys"]
    # 可能是字符串数组，也可能是对象数组
    if isinstance(ak, list) and len(ak) > 0 and isinstance(ak[0], dict):
        return [x.get("pubkey") for x in ak]
    return ak

def _is_signer(tx: Dict[str, Any], wallet: str) -> bool:
    ak = tx["transaction"]["message"]["accountKeys"]
    if isinstance(ak, list) and len(ak) > 0 and isinstance(ak[0], dict):
        for x in ak:
            if x.get("pubkey") == wallet and x.get("signer") is True:
                return True
        return False
    # 回退：如果不是对象，无法判断 signer；通常 v0 会是对象
    # 但 logsSubscribe 已保证这笔交易与 wallet 强相关，仍继续
    return True

def _sol_delta_for_wallet(tx: Dict[str, Any], wallet: str) -> int:
    """post - pre，单位 lamports。负数=花了SOL"""
    ak = _account_keys_list(tx)
    try:
        idx = ak.index(wallet)
    except ValueError:
        return 0
    pre = tx["meta"]["preBalances"][idx]
    post = tx["meta"]["postBalances"][idx]
    return int(post) - int(pre)

WSOL_MINT = "So11111111111111111111111111111111111111112"

# def _sol_delta_for_wallet(tx: dict, wallet: str) -> int:
#     """
#     返回系统账户 SOL 余额变化（lamports）
#     """
#     pre = tx["meta"].get("preBalances", [])
#     post = tx["meta"].get("postBalances", [])
#     accounts = tx["transaction"]["message"]["accountKeys"]

#     if wallet in accounts:
#         idx = accounts.index(wallet)
#         return post[idx] - pre[idx]
#     return 0


def _token_delta_for_wallet(tx: dict, wallet: str, token_mint: str) -> int:
    """
    返回某 SPL Token 在钱包里的余额变化（以最小单位计算）
    """
    pre = tx["meta"].get("preTokenBalances", [])
    post = tx["meta"].get("postTokenBalances", [])

    pre_map = {}
    for b in pre:
        if b["owner"] == wallet and b["mint"] == token_mint:
            pre_map[b["accountIndex"]] = int(b["uiTokenAmount"]["amount"])

    post_map = {}
    for b in post:
        if b["owner"] == wallet and b["mint"] == token_mint:
            post_map[b["accountIndex"]] = int(b["uiTokenAmount"]["amount"])

    # 取差值（同一个 accountIndex 对比）
    delta = 0
    for idx, pre_amt in pre_map.items():
        post_amt = post_map.get(idx, pre_amt)
        delta += post_amt - pre_amt

    return delta


def get_spent_amount(tx: dict, wallet: str) -> int:
    """
    统一计算领导花了多少（优先 SOL，否则查 wSOL）
    返回 lamports / wSOL 最小单位数量
    """
    sol_delta = _sol_delta_for_wallet(tx, wallet)
    if sol_delta < 0:
        return sol_delta

    # 没花 SOL，就看 wSOL
    wsol_delta = _token_delta_for_wallet(tx, wallet, WSOL_MINT)
    if wsol_delta < 0:
        return wsol_delta

    return 0


def _token_deltas_for_wallet(tx: Dict[str, Any], wallet: str) -> Dict[str, int]:
    """
    返回 {mint: delta_amount_in_base_units}，仅统计 owner==wallet 的变化。
    delta=post - pre（基础单位整数）。正数=增持，负数=减持
    """
    deltas: Dict[str, int] = {}
    pre_list = tx["meta"].get("preTokenBalances", []) or []
    post_list = tx["meta"].get("postTokenBalances", []) or []

    def key(t):
        return (t.get("accountIndex"), t.get("mint"), t.get("owner"))

    pre_map = {}
    for t in pre_list:
        if t.get("owner") != wallet:
            continue
        pre_map[key(t)] = int(t["uiTokenAmount"]["amount"])  # 基础单位整数

    post_map = {}
    for t in post_list:
        if t.get("owner") != wallet:
            continue
        post_map[key(t)] = int(t["uiTokenAmount"]["amount"])

    # union keys
    keys = set(pre_map.keys()) | set(post_map.keys())
    for k in keys:
        _, mint, _ = k
        pre_amt = pre_map.get(k, 0)
        post_amt = post_map.get(k, 0)
        delta = post_amt - pre_amt
        if delta != 0:
            deltas[mint] = deltas.get(mint, 0) + delta
    return deltas

def classify_follow_action(tx: Dict[str, Any], leader: str) -> Optional[Tuple[str, str, int]]:
    """
    判定是否领导买入/卖出：
    - 必须领导是 signer（排除空投）
    - 看领导 SOL 余额 delta 与 token delta。
    返回: ("buy"|"sell", token_mint, abs(token_delta))
    """
    if tx is None or tx.get("meta") is None or tx["meta"].get("err") is not None:
        return None

    if not _is_signer(tx, leader):
        return None  # 不是领导主动签名，忽略

    sol_delta = get_spent_amount(tx, leader)  # lamports，负数=花了SOL
    token_deltas = _token_deltas_for_wallet(tx, leader)

    # 过滤掉 wSOL 本身，和 0 变动
    # 只关心非 SOL 的 SPL 代币
    candidates = [(m, d) for (m, d) in token_deltas.items() if m != SOL_MINT and d != 0]
    if not candidates:
        return None

    # 规则：
    # - 如果有某个 mint delta > 0，且 sol_delta < 0（花了SOL），视为买入
    # - 如果 mint delta < 0，且 sol_delta > -? 这里我们仅按 token 减少判定为卖出（领导可能换回 SOL 或换别的）
    buy_mints = [(m, d) for (m, d) in candidates if d > 0]
    sell_mints = [(m, d) for (m, d) in candidates if d < 0]

    # 买入优先：同时出现时以增持为“买入”
    if buy_mints and sol_delta < 0:
        # 若多种 mint 同时增持，取绝对变动量最大的一个
        m, d = max(buy_mints, key=lambda x: x[1])
        return ("buy", m, abs(d))

    if MIRROR_SELL and sell_mints:
        m, d = min(sell_mints, key=lambda x: x[1])  # d 为负，abs最大
        return ("sell", m, abs(d))

    return None

# ================= Jupiter 下单（维持你的签名方式） =================
async def jupiter_swap(input_mint: str, output_mint: str, amount_in_base_units: int) -> Optional[str]:
    """
    基于 solders：
      1) quote
      2) swap (拿到 base64 交易)
      3) VersionedTransaction.from_bytes
      4) 用 FOLLOWER_KEYPAIR 完成签名
      5) send_raw_transaction(bytes(tx))
    返回签名字符串或 None
    """
    try:
        async with aiohttp.ClientSession() as session:
            # 1) 报价（amount 用基础单位：SOL=lamports）
            quote_url = (
                "https://quote-api.jup.ag/v6/quote"
                f"?inputMint={input_mint}&outputMint={output_mint}"
                f"&amount={amount_in_base_units}&slippageBps={int(SLIPPAGE_TOLERANCE*10000)}"
            )
            async with session.get(quote_url, proxy=PROXY) as r:
                quote = await r.json()
                print("✅ Quote:", quote)
                if quote.get("error") or not quote.get("routePlan"):
                    print("⚠️ 报价失败，跳过")
                    return None

            # 2) swap，Jupiter v6 直接用 quoteResponse
            swap_url = "https://quote-api.jup.ag/v6/swap"
            body = {
                "quoteResponse": quote,
                "userPublicKey": FOLLOWER_PUBKEY,
                "wrapUnwrapSOL": True,
            }
            async with session.post(swap_url, json=body, proxy=PROXY) as r:
                swap_tx = await r.json()
                print("✅ SwapTX:", swap_tx)
                if "swapTransaction" not in swap_tx:
                    print("⚠️ 未拿到 swapTransaction")
                    return None
                tx_b64 = swap_tx["swapTransaction"]

        # 3) 反序列化 → 4) 用 solders.Keypair 完成签名
        tx_bytes = base64.b64decode(tx_b64)
        unsigned_tx = VersionedTransaction.from_bytes(tx_bytes)
        signed_tx = VersionedTransaction(unsigned_tx.message, [FOLLOWER_KEYPAIR])  # 关键：传 Keypair，而不是 Signature
        raw = bytes(signed_tx)

        # 5) 广播
        async with AsyncClient(RPC_URL) as client:
            resp = await client.send_raw_transaction(raw)  # 返回 {'result': '<sig>', ...}
            if hasattr(resp, "value"):
                sig = str(resp.value)  # 转成 base58 字符串
                print(f"🚀 已广播: {sig}")
                return sig
            else:
                sig = resp.get("result") if isinstance(resp, dict) else str(resp)
                print(f"🚀 已广播: {sig}")
                return sig

    except Exception as e:
        print(f"❌ Jupiter 下单异常: {e}")
        return None

# ⬇️ 回查链上交易，解析实际到账数量
from solders.signature import Signature
async def fetch_received_amount(sig: str, token_mint: str) -> int:
    """回查交易，获取买入代币数量（以最小单位计数，例如 6 位小数的 token 就是整数 lamports）"""
    async with AsyncClient(RPC_URL) as client:
        sig_obj = Signature.from_string(sig)
        tx = await client.get_transaction(
            sig_obj,
            encoding="jsonParsed",
            commitment="finalized",   # 确保是最终确认
            max_supported_transaction_version=0
        )

        if tx.value is None:
            print(f"⚠️ 交易 {sig} 还未确认或查询失败")
            return 0

        meta = tx.value.transaction.meta
        if meta is None:
            print(f"⚠️ 交易 {sig} 没有 meta")
            return 0

        # 优先用 postTokenBalances
        balances = meta.post_token_balances
        if not balances:
            print(f"⚠️ 交易 {sig} 没有 postTokenBalances")
            return 0

        for b in balances:
            if compare_token_mints(b.mint, token_mint):
                try:
                    return int(b.ui_token_amount.amount)  # 原始整数数量
                except Exception:
                    print(f"⚠️ 未能获取到账数量 {token_mint}")
                    pass

        print(f"⚠️ 未能获取到账数量 {token_mint}")
        return 0

def compare_token_mints(balance_mint: Pubkey, target_mint: str) -> bool:
    """安全比较代币地址（处理所有格式情况）"""
    try:
        # 将目标地址转为Pubkey对象比较
        target_pubkey = Pubkey.from_string(target_mint.strip())
        return balance_mint == target_pubkey
    except Exception as e:
        print(f"⚠️ 地址比较异常: {e}")
        return False

# ⬇️ 回查链上交易，解析实际持有token数量
async def get_token_balance(wallet_pubkey: str, token_mint: str) -> int:
    """查询某个钱包的 SPL Token 余额"""
    async with AsyncClient(RPC_URL) as client:
        resp = await client.get_token_accounts_by_owner(
            Pubkey.from_string(wallet_pubkey),
            TokenAccountOpts(mint=Pubkey.from_string(token_mint))
        )

        print("=== resp 原始返回 ===")
        print(resp)
        for keyed_acc in resp.value:
            try:
                acc = keyed_acc.account
                data = bytes(acc.data)
                # mint: 32 bytes, owner: 32 bytes, amount: 8 bytes (u64, little-endian)
                mint_bytes = data[:32]
                amount_bytes = data[64:72]
                mint_str = str(Pubkey(mint_bytes))
                if mint_str == token_mint:
                    amount = struct.unpack("<Q", amount_bytes)[0]
                    return amount
            except Exception as e:
                print(f"❌ 解析 token account 失败: {e}")
        return 0

# ================= 跟单执行器 =================
async def follow_buy(token_mint: str, leader_spent_lamports: int):
    # 冷却
    if token_mint in _last_action_at and now_ts() - _last_action_at[token_mint] < COOLDOWN_SEC:
        return
    _last_action_at[token_mint] = now_ts()

    # 计算我们要花多少：跟随比例 + 单笔上限 + 预留
    to_spend = int(leader_spent_lamports * FOLLOW_RATIO)
    max_lamports = int(MAX_PER_TRADE_SOL * LAMPORTS_PER_SOL)
    to_spend = min(to_spend, max_lamports)

    bal = await rpc_get_balance(FOLLOWER_PUBKEY)
    free = max(0, bal - int(MIN_SOL_RESERVE * LAMPORTS_PER_SOL))
    if free <= 0:
        print("⚠️ 余额不足（预留后无可用），跳过")
        return
    to_spend = min(to_spend, free)
    if to_spend <= 0:
        print("⚠️ 计算后 to_spend=0，跳过")
        return

    print(f"🟢 跟单买入 {token_mint}，花费 {to_spend / LAMPORTS_PER_SOL:.6f} SOL")
    sig = await jupiter_swap(SOL_MINT, token_mint, to_spend)
    if sig:
        # 等待交易确认
        await asyncio.sleep(15)  # 简单粗暴：等 15 秒
        # 查询到账数量（需要你已经有 fetch_received_amount 函数）
        recv_qty = await fetch_received_amount(sig, token_mint)

        if recv_qty > 0:
            pos = POSITIONS.get(token_mint, {"qty": 0, "cost_lamports": 0, "sell_step": 0})
            pos["qty"] += recv_qty
            pos["cost_lamports"] += to_spend
            pos["last_sig"] = sig
            POSITIONS[token_mint] = pos
            save_positions(POSITIONS)

            print(f"✅ 买入成功，到账 {recv_qty} 个 {token_mint}，累计持仓 {pos['qty']}")
        else:
            pos = POSITIONS.get(token_mint, {"qty": 0, "cost_lamports": 0})
            pos["cost_lamports"] += to_spend
            pos["last_sig"] = sig
            POSITIONS[token_mint] = pos
            save_positions(POSITIONS)
            print(f"⚠️ {token_mint} 买入交易成功但未查询到到账数量")

async def follow_sell(token_mint: str):
    if not MIRROR_SELL:
        return

    # 冷却
    if token_mint in _last_action_at and now_ts() - _last_action_at[token_mint] < COOLDOWN_SEC:
        return
    _last_action_at[token_mint] = now_ts()

    pos = POSITIONS.get(token_mint)
    if not pos:
        print(f"ℹ️ 未记录 {token_mint} 数量，跳过卖出")
        return

    # === 关键优化：实时查链上余额 ===
    chain_qty = await get_token_balance(FOLLOWER_PUBKEY, token_mint)
    if chain_qty <= 0:
        print(f"⚠️ 链上 {token_mint} 余额为 0，清理本地持仓记录")
        POSITIONS.pop(token_mint, None)
        save_positions(POSITIONS)
        return

    # 用链上余额覆盖本地 qty，保证准确
    pos["qty"] = chain_qty

    qty = pos["qty"]
    if qty <= 0:
        print(f"ℹ️ {token_mint} 持仓为 0，跳过卖出")
        POSITIONS.pop(token_mint, None)
        save_positions(POSITIONS)
        return

    step = pos.get("sell_step", 0)
    if step >= len(SELL_STEPS):
        print(f"ℹ️ {token_mint} 已完成所有分批卖出")
        return

    # 计算卖出数量
    if step == 0:
        sell_qty = int(qty * SELL_STEPS[step])  # 第一次按总仓位
    else:
        sell_qty = int(qty * SELL_STEPS[step])  # 后续按剩余仓位
    if sell_qty <= 0:
        print(f"ℹ️ {token_mint} 分批卖出数量为 0，跳过")
        return

    print(f"🔴 分批卖出 {token_mint} 第 {step+1} 次，数量(基础单位)：{sell_qty}")
    sig = await jupiter_swap(token_mint, SOL_MINT, sell_qty)
    if not sig:
        print(f"⚠️ {token_mint} 卖出失败（第 {step+1} 步）")
        return

    # 更新仓位和步骤
    qty -= sell_qty
    if qty <= 0 or step == len(SELL_STEPS) - 1:
        POSITIONS.pop(token_mint, None)  # 卖完清空
        print(f"✅ {token_mint} 已全部卖出完成")
    else:
        pos["qty"] = qty
        pos["sell_step"] = step + 1
        POSITIONS[token_mint] = pos

    save_positions(POSITIONS)

async def get_token_holders(token_mint: str, helius_limit: int = 100, rpc_limit: int = 20) -> list[str]:
    """
    获取某个 SPL Token 的前 holders
    优先 Helius API（最多 helius_limit 个）
    如果失败，退回 RPC 的 getTokenLargestAccounts（最多 rpc_limit 个）
    """
    # 1️⃣ 尝试 Helius
    helius_url = f"https://api.helius.xyz/v0/token-holders?api-key={API_KEY}&mint={token_mint}&limit={helius_limit}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(helius_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    holders = [h["owner"] for h in data if h.get("owner")]
                    if holders:
                        return holders
    except Exception as e:
        print(f"⚠️ Helius 查询失败: {e}")

    # 2️⃣ Helius 失败或空 → 回退 RPC
    try:
        async with AsyncClient(RPC_URL) as client:
            resp = await client.get_token_largest_accounts(PublicKey(token_mint))
            if resp.value:
                holders = [str(acc.address) for acc in resp.value[:rpc_limit]]
                return holders
    except Exception as e:
        print(f"⚠️ RPC 查询失败: {e}")

    return []  # 两种方式都失败

async def adjust_action_with_wallets(kind: str, mint: str, sol_delta: int) -> tuple[bool, int]:
    """
    根据白名单/黑名单调整买入金额:
    - 如果黑名单持有人存在，返回 (False, sol_delta)，表示跳过
    - 如果白名单持有人存在，放大买入金额
    - 否则原样返回
    """
    holders = await get_token_holders(mint)
    if not holders:
        return True, sol_delta  # 查不到就默认不调整

    if any(h in BLACKLIST_WALLETS for h in holders):
        print(f"🚫 {mint} 持有人包含黑名单，跳过买入")
        return False, sol_delta

    if any(h in VIP_WALLETS for h in holders):
        boosted = int(sol_delta * weighted_ratio)  # 加权
        print(f"⭐ {mint} 持有人包含 VIP，买入金额翻倍 {sol_delta} → {boosted}")
        return True, boosted

    return True, sol_delta

# ================= 日志订阅（推荐替代 accountSubscribe） =================
async def listen_leader_logs():
    """
    用 logsSubscribe 订阅所有与领导地址相关的交易日志，
    收到签名后 getTransaction 再判定是否买入/卖出。
    自动处理断线重连和心跳。
    """
    sub_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "logsSubscribe",
        "params": [
            {"mentions": [SMART_WALLET]},
            {"commitment": "confirmed"}
        ]
    }

    while True:  # 无限循环，断开后自动重连
        try:
            async with websockets.connect(
                WSS_URL,
                ping_interval=20,   # 每 20 秒发送心跳包
                ping_timeout=10     # 10 秒内未响应则判定断开
            ) as ws:
                await ws.send(json.dumps(sub_msg))
                print("✅ 已订阅领导日志（logsSubscribe）")

                async for raw in ws:
                    try:
                        data = json.loads(raw)
                        if "params" not in data:
                            continue

                        val = data["params"]["result"]["value"]
                        sig = val.get("signature")
                        if not sig:
                            continue

                        # 拉取交易细节并分类
                        tx = await rpc_get_transaction(sig)
                        action = classify_follow_action(tx, SMART_WALLET)
                        if not action:
                            continue

                        kind, mint, delta = action

                        # 计算领导花了多少 SOL（仅买入用得到）
                        sol_delta = get_spent_amount(tx, SMART_WALLET)
                        if kind == "buy":
                            leader_spent = abs(sol_delta) if sol_delta < 0 else int(0.01 * LAMPORTS_PER_SOL)
                            
                            # ✅ 这里加白名单/黑名单逻辑
                            allow, leader_spent = await adjust_action_with_wallets(kind, mint, leader_spent)
                            if not allow:
                                continue
                            # await asyncio.sleep(60)  # 等待买单确认，这里可以加入你的逻辑
                            await follow_buy(mint, leader_spent)
                        elif kind == "sell":
                            await follow_sell(mint)

                    except Exception as e:
                        print(f"❌ 日志处理异常: {e}")

        except ConnectionClosedError as e:
            print(f"⚠️ WebSocket 连接断开: {e}，3 秒后重连...")
            await asyncio.sleep(3)
        except Exception as e:
            print(f"⚠️ 监听异常: {e}，5 秒后重试...")
            await asyncio.sleep(5)

# ================= 主程序 =================
async def main():
    print(f"👤 Leader: {SMART_WALLET}")
    print(f"👤 Follower: {FOLLOWER_PUBKEY}")
    print(f"⚙️ 资金管理: ratio={FOLLOW_RATIO}, max_per_trade={MAX_PER_TRADE_SOL} SOL, reserve={MIN_SOL_RESERVE} SOL")
    await listen_leader_logs()

if __name__ == "__main__":
    asyncio.run(main())

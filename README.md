# Solana auto-trading tool

```markdown
# Solana Auto-Trading Copy Bot 🤖

An intelligent copy-trading system built on the Solana blockchain, designed to automatically mirror the token buy and sell operations of a designated "smart wallet".

## Key Features ✨

- **Real-time Monitoring**: Tracks the transaction activity of a target wallet in real-time via WebSocket.
- **Auto Copy-Trading**: Automatically replicates the buy and sell actions of the target wallet.
- **Capital Management**: Configurable follow ratio, maximum trade size, and minimum SOL reserve.
- **Staggered Selling**: Supports selling positions in multiple batches to optimize returns.
- **Whitelist/Blacklist**: Filters and weights trades based on token holders.
- **Cooldown Mechanism**: Prevents repeated operations on the same token.
- **Persistent Storage**: Automatically saves position status; data persists after restart.

## Installation & Dependencies 📦

```bash
pip install solders aiohttp websockets
```

## Configuration ⚙️

Modify the following configurations at the beginning of the code:

```python
# API Configuration
API_KEY = "Your_Helius_API_Key"  # Obtain from https://helius.xyz

# Wallet Configuration
SMART_WALLET = "Smart_Wallet_Address_To_Follow"  # Leader wallet
FOLLOWER_SECRET = os.getenv("FOLLOWER_SECRET", "Your_Follower_Wallet_Private_Key")

# Trading Parameters
SLIPPAGE_TOLERANCE = 0.128  # Slippage tolerance (12.8%)
FOLLOW_RATIO = 0.01         # Follow ratio (1%)
MAX_PER_TRADE_SOL = 0.18    # Maximum SOL per trade
MIN_SOL_RESERVE = 0.02      # Minimum SOL reserve

# Proxy Configuration (if needed)
PROXY = "http://127.0.0.1:PORT"  # Or set to None
```

## Usage Guide 🚀

### 1. Get a Helius API Key
Visit [Helius](https://helius.xyz) to register and get free RPC services.

### 2. Prepare Follower Wallet
- Create a new Solana wallet specifically for copy-trading.
- Fund it with a small amount of SOL for trading.
- **Important**: Do not use your main wallet holding significant funds.

### 3. Set Environment Variable (Recommended)
```bash
export FOLLOWER_SECRET="Your_Follower_Wallet_Private_Key"
```

### 4. Run the Bot
```bash
python swap.py
```

## Core Configuration Details 🔧

### Capital Management
```python
FOLLOW_RATIO = 0.01      # Follow ratio: Our Spend = Leader Spend × 1%
MAX_PER_TRADE_SOL = 0.18 # Maximum spend per trade: 0.18 SOL
MIN_SOL_RESERVE = 0.02   # Always keep at least 0.02 SOL reserved
```

### Selling Strategy
```python
SELL_STEPS = [0.25, 0.40, 0.50, 0.50, 1.00]  # Staggered sell ratios
MIRROR_SELL = True       # Whether to mirror sell operations
COOLDOWN_SEC = 6         # Cooldown period for the same token
```

### List System
```python
VIP_WALLETS = {"VIP_Wallet_Address"}        # Whitelist: Increase buy size if holder exists
BLACKLIST_WALLETS = {"Blacklisted_Address"} # Blacklist: Skip trade if holder exists
weighted_ratio = 2                          # Whitelist multiplier
```

## Workflow 🔄

1.  **Listen**: Subscribe to the target wallet's transaction logs via WebSocket.
2.  **Analyze**: Parse transactions to identify buy/sell operations and target tokens.
3.  **Filter**: Check if token holders are in the whitelist/blacklist.
4.  **Execute**: Perform the copy trade via Jupiter API.
5.  **Record**: Update local position status and transaction history.

## File Structure 📁

```
├── swap.py                 # Main program file
├── positions.json          # Position records (auto-generated)
├── README.md               # This documentation
└── requirements.txt        # Dependencies list
```

## Risk Warning ⚠️

1.  **Capital Risk**: Only invest funds you are prepared to lose entirely.
2.  **Technical Risk**: The code may contain bugs; test with small amounts first.
3.  **Market Risk**: Copy-trading does not guarantee profits; losses may occur.
4.  **Regulatory Risk**: Ensure compliance with local laws and regulations.

## Frequently Asked Questions ❓

**Q: How do I choose a "smart wallet" to follow?**
A: It is recommended to choose well-known trader wallets with a long-term record of profitability and a stable trading style.

**Q: What is the typical copy-trading delay?**
A: With Helius' free RPC node, execution typically happens within seconds or tens of seconds, depending on network conditions. The speed is not determined by this tool itself but by the responsiveness of your Helius node. Theoretically, millisecond-level speed is possible.

**Q: Does it support following multiple wallets simultaneously?**
A: The current version supports a single wallet. The code can be modified to enable multi-wallet copying.

**Q: How can I monitor the bot's status?**
A: The program outputs detailed transaction logs to the console.

## Support 💬

Please submit an Issue or contact the developer if you have any problems.
Official Twitter: [@opulse_protocol](https://x.com/opulse_protocol/)

## License 📄

MIT License

---

**Disclaimer**: This tool is for technical learning and exchange purposes only. Users bear full responsibility for any financial losses incurred during its use.

If you find this project helpful, please consider giving it a ⭐️ **Star** to show your support. If it saves you time or solves a big problem, you're also welcome to **buy us a coffee** ☕️, which will motivate me to continue maintaining and updating it.

**[Sponsor Me]**: Solana Wallet Address: rAyqwMpQF85DkWqNNMFytV5MB2GGhEhXqLKB2V6gf8p
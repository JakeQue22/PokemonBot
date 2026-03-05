# PokemonBot

Product availability monitor with cycling proxies, queue detection, and multi-site support.
Built for tracking Pokemon Center drops (e.g. Ascending Heroes) and any other retailer.

## Features

- **Pokemon Center monitor** – detects stock status, add-to-cart signals, Queue-it waiting rooms, and price
- **Generic monitor** – keyword-based stock detection that works with any website
- **Proxy rotation** – round-robin cycling through HTTP/HTTPS/SOCKS4/SOCKS5 proxies loaded from a file
- **User-Agent rotation** – randomised browser fingerprints on every request
- **Discord notifications** – real-time webhook alerts with rich embeds (product name, price, link)
- **Concurrent tasks** – monitor many URLs at once with configurable concurrency limits
- **Extensible** – add new site monitors by subclassing `BaseMonitor`

## Quick Start

```bash
# 1. Install
pip install -e .

# 2. Generate a starter config
pokemonbot init

# 3. (Optional) Add proxies – one per line
#    Supported: http://host:port, socks5://user:pass@host:port, host:port:user:pass
echo "http://1.2.3.4:8080" > proxies.txt

# 4. Edit config.yaml with your target URLs, keywords, and Discord webhook

# 5. Run
pokemonbot run
```

## CLI Reference

| Command             | Description                                       |
|---------------------|---------------------------------------------------|
| `pokemonbot run`    | Start all monitors defined in `config.yaml`       |
| `pokemonbot init`   | Create a starter `config.yaml`                    |
| `pokemonbot sites`  | List available site monitor plugins                |
| `pokemonbot check URL` | One-shot check of a single URL                 |

### Options

```
pokemonbot run -c custom_config.yaml   # custom config path
pokemonbot run -v                      # verbose / debug logging
pokemonbot check URL -s pokemoncenter  # use Pokemon Center parser
pokemonbot check URL -k "Ascending Heroes"  # keyword filter
```

## Configuration

See [`config.example.yaml`](config.example.yaml) for a fully commented template.

### Proxy File Format

Create a `proxies.txt` file (path configurable in `config.yaml`):

```
# HTTP proxies
http://12.34.56.78:8080
http://user:pass@12.34.56.78:8080

# SOCKS5 proxies
socks5://12.34.56.78:1080
socks5://user:pass@12.34.56.78:1080

# Shorthand (defaults to http)
12.34.56.78:8080
12.34.56.78:8080:user:pass
```

### Discord Webhook

1. In your Discord server go to **Server Settings → Integrations → Webhooks**
2. Create a new webhook, copy the URL
3. Paste it into `config.yaml` under `notifier.discord_webhook_url`

Alerts include the product name, status, price, and a direct link.

## Architecture

```
pokemonbot/
├── cli.py        # Click CLI (run / init / sites / check)
├── config.py     # YAML config loading with env-var expansion
├── proxy.py      # Proxy parsing, loading, and round-robin pool
├── session.py    # aiohttp sessions with proxy + UA rotation
├── monitor.py    # Site-specific parsers (PokemonCenter, Generic)
├── notifier.py   # Console + Discord webhook alert pipeline
└── tasks.py      # Async task manager running monitors concurrently
```

## Adding a New Site Monitor

```python
from pokemonbot.monitor import BaseMonitor, MONITOR_REGISTRY
from pokemonbot.notifier import Alert

class BestBuyMonitor(BaseMonitor):
    site_name = "bestbuy"

    def parse(self, response, *, url, keywords):
        body = response["body"]
        if not self._keyword_match(body, keywords):
            return None
        if "add-to-cart" in body.lower():
            return Alert(product_name=url, url=url, status="in_stock", site=self.site_name)
        return None

# Register it
MONITOR_REGISTRY["bestbuy"] = BestBuyMonitor
```

Then use `site: bestbuy` in your config.

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## License

MIT
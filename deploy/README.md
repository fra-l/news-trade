# Deployment on the Raspberry Pi (systemd user units)

Prerequisites: `redis-server` running (apt), `uv sync`, and a `.env` with the
Alpaca paper keys and LLM settings (see `.env.example`).

```sh
systemctl --user link ~/news-trade/deploy/systemd/news-trade.service
systemctl --user start news-trade      # manual start; not enabled at boot
systemctl --user stop news-trade
journalctl --user -u news-trade -f
```

`loginctl enable-linger $USER` lets user units run without an open login.

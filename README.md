# aftercare

**Your agent checks its own work after the fact.**

"Deployed. The shop is live at https://shop.example.com." It was true when the agent said it. Is it still true on Thursday?

Every agent tool checks work at the moment it's done. Aftercare comes back later:
- **It remembers the address.** When a reply says something is deployed or live at a public address, Aftercare notes it.
- **It checks again.** Every few hours, at the start of a session, it checks each address with one plain request.
- **It tells you.** If something the agent said was working has stopped, the agent is told at the start of your next turn, and it tells you first:

```text
[Aftercare] Something you told the user was working has changed since:
- https://shop.example.com stopped working after you said on 10 Nov 14:02:
  "Deployed. The shop is live at https://shop.example.com." Now: HTTP 500.
```

You hear when it's back, too.

## Use it

You don't need to do anything: addresses are picked up from the agent's replies. To see or change what's watched:

```text
/aftercare                 what is watched, and how each one looked last time
/aftercare check           check everything now
/aftercare watch <url>     watch an address yourself
/aftercare forget <n|url>  stop watching one
hermes aftercare check     the same from your shell
```

`hermes aftercare check` works from cron, so a server can be checked even when you aren't chatting.

```text
/aftercare check

Checked 2: 1 answering, 1 not.
  ✅ https://example.com  HTTP 200, 281 ms
  ❌ https://httpbin.org/status/500  HTTP 500, 875 ms  <- has not worked yet
```

## What counts as a claim

A sentence in a reply that says something is **deployed, live, published, launched, up and running, or available, hosted or running at** a public `http(s)` address.

These are ignored:
- links to docs and references
- negated or conditional sentences ("not live yet", "once DNS updates it will be live")
- questions
- text inside code blocks

## Safety

- **It never checks localhost, private networks or local names.** It skips `127.0.0.1`, `10.x`, `192.168.x`, `::1`, `.local` and `.internal`. It also skips any public name that resolves to a private address, and any redirect that leads to one.
- **One GET request per address,** reading at most 1 KB, with a timeout (8 seconds by default). It never runs commands, and never sends anything except the request itself.
- **Checks run in the background,** so starting a session never waits for the network.
- **Certificates are verified.** Aftercare uses the `certifi` bundle when it is installed, because some Python builds ship without a list of trusted certificate authorities.

## What it touches

- **Network:** one GET request to each watched address, per check, with the user agent `hermes-aftercare/0.1`.
- **Adds context to your next turn** (through `pre_llm_call`) when a watched address changes state. It does this only once per change.
- **Stores locally:** each address, the sentence that claimed it, and the last six check results, in `<hermes home>/plugin-data/aftercare/aftercare.json`.
- **No model calls, no telemetry, no dependencies.**

## Settings

```yaml
plugins:
  entries:
    aftercare:
      settings:
        auto_watch: true    # pick up addresses from the agent's replies
        every_hours: 6      # how often each address is checked
        keep_days: 30       # forget an address 30 days after it was last claimed
        max_watches: 25     # most addresses watched at once
        timeout: 8          # seconds to wait for an answer
```

## Limits

- **Up means the address answers with 2xx or 3xx.** A page that loads but shows the wrong content still counts as up.
- **Checks happen when a session starts** (or from cron, or with `/aftercare check`). If you don't use Hermes for a week, nothing is checked that week unless cron runs it.
- **English replies only,** for picking up claims automatically. `/aftercare watch` works for anything.

## Development

```bash
pytest tests
```

37 tests cover finding claims, ignoring links and private addresses, state changes (down, never up, back), alerting only once, background checks, the commands, expiry, limits and the private-address refusals. The network is scripted, so no test sends real requests.

## License

MIT

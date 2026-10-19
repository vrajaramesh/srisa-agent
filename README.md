# srisa-agent

WhatsApp sales agent for SRISA Boutique. **Current stage:** an AI sales assistant (Claude) answers customers on WhatsApp using read-only catalog tools backed by mybill-service. Every conversation and message is stored in Postgres and processed through a queue.

```
Meta ──POST──▶ /webhook/whatsapp ── verify signature ─▶ save to DB ─▶ enqueue ─▶ 200
                                                                         │
             Cloud Tasks (prod) / BackgroundTask (local) ◀───────────────┘
                     │
                     ▼
            /tasks/process-message ─▶ worker: lock conversation ─▶ reply ─▶ save ─▶ commit
```

## Layout

```
app/
  main.py            FastAPI app: webhook (verify → save → enqueue), worker endpoint
  worker.py          process_conversation(): lock → AI agent → send → record (one run per batch of messages)
  agent/runner.py    Claude tool-use loop (retry once, fallback, refusal fallback, limits)
  agent/context.py   cached system prompt + per-turn facts + history
  agent/prompts/system.md   THE PROMPT - edit freely
  agent/tools.py     get_design, list_latest_designs, get_size_chart, send_design_photos, set_current_design, request_human_help
  mybill_client.py   read-only client for mybill-service /api/agent/** (Google ID token)
  queue.py           Cloud Tasks enqueue, BackgroundTasks fallback
  repositories.py    All DB queries
  models.py          SQLAlchemy models (schema "agent")
  db.py              Async engine tuned for Neon's pooler
  config.py          Settings from env vars
  security.py        Meta signature check, verify token, Cloud Tasks OIDC check
  whatsapp_client.py send_text(), mark_as_read()
  webhook_parser.py  Payload → IncomingMessage / StatusUpdate
  logging_setup.py   JSON logs for Cloud Logging
alembic/             Migrations (create schema "agent" + tables)
tests/
```

## Database

The bot uses the **same Neon database as mybill-service** (`neondb`). All of its tables are in their own schema, **`agent`**: `conversations`, `messages`, `product_links` and `ref_codes`, plus Alembic's `alembic_version`. Nothing else in the database is touched.

| Variable | Used by | Value |
|---|---|---|
| `DATABASE_URL` | the app | Neon **pooled** URL (host contains `-pooler`), `?sslmode=require` |
| `DATABASE_URL_DIRECT` | Alembic only | Neon **direct** URL (no `-pooler`) |
| `DB_SCHEMA` | both | `agent` (tests use `agent_test`) |

Notes:
- `app/db.py` turns `sslmode=require` into asyncpg's `ssl` argument.
- It disables prepared-statement caches, because Neon's pooler is PgBouncer in transaction mode.
- It only uses transaction-scoped advisory locks.

**Message statuses:**
- Inbound: `received → processed`, or `failed` (retried up to 5 times), or `skipped` (sender not in `ALLOWED_SENDERS`).
- Outbound: `sent → delivered → read`, or `failed`. These come from Meta's receipts and only ever move forward.

## AI agent

- **Model:** `claude-sonnet-5-5` at effort `low` (fast chat). The server-side refusal fallback is on (`fallbacks: "default"`).
- **Caching:** the tool definitions and `prompts/system.md` are cached. Per-turn facts (customer name, current design, date) come after the cache breakpoint. `agent.llm_turns.cache_read_tokens` should be above 0 from the second message of a conversation.
- **One run per batch:** all of a customer's pending messages are answered in one run, and the newest `HISTORY_MESSAGES` messages are sent as context.
- **Design codes:** a code in the text (e.g. `kur-101`) becomes the conversation's current design before Claude is called, if mybill knows it.
- **Handoff:** the `request_human_help` tool, or any failure, sets `needs_human = true` and logs a `HUMAN_HANDOFF` warning. While it's set, messages are stored as `skipped`, left unread, and the bot stays silent. To give the conversation back to the bot:

  ```bash
  curl -X POST -H "Authorization: Bearer $ADMIN_API_KEY" https://srisa-agent-276371417285.asia-south1.run.app/admin/conversations/<conversation-uuid>/resume
  ```

  Find handed-over chats with `SELECT id, external_user_id, needs_human_reason FROM agent.conversations WHERE needs_human;`
- **Failures:**
  - A Claude timeout or 5xx is retried once.
  - If it fails again, or the reply is a refusal or a limit is hit, the customer gets *"Thank you! Our team will get back to you shortly 🙏"* and the chat is handed over.
  - If mybill is down, tools return `temporarily_unavailable` and the prompt tells Claude to hand over.
- **Cost and debugging:** `agent.llm_turns` has one row per run (tokens, cache, tool calls with their duration, latency, errors).

### Order flow (code controls the order)

```
browsing ─start_order─▶ sizing ─(size+height+fit)─▶ customise ─finish_customisation─▶ address ─(details+quote)─▶ confirm ─(Confirm tap / "yes")─▶ order_created
```

- **`agent/flow.py`** is the state machine. Only code changes `conversations.stage`. Every change to a draft increases `order_drafts.version` and makes any summary sent earlier non-confirmable.
- **Tools are limited by stage.** The runner offers only the current stage's tools (`tools.tools_for_stage`), and each handler checks the stage again.
- **Buttons and lists:** sizes, fit and options are sent as WhatsApp buttons or lists. Taps on `size:` / `fit:` / `opt:` ids are applied by code before Claude runs.
- **Confirmation is code, not a tool** (`agent/orders.py`, called by the worker before the model).
  - The order is created only by a tap on `confirm:<draft>:<version>`, or a short "yes" right after the summary for that same version.
  - mybill `create_order` is called with `idempotency_key = "<draft_id>:<version>"`, so a double tap or a retry never creates a second order or a second confirmation message.
  - If she confirms an older version, the current summary is re-sent instead.
- **The model never sees** customer ids, idempotency keys or totals it didn't receive from a tool result.

### Payments (mybill owns them)

- **On confirmation,** the confirmation message includes mybill's Razorpay Payment Link (`POST /api/agent/orders/{no}/payment-link`, which is idempotent).
- **Tools:** `get_order_status` answers questions about order and payment status. `resend_payment_link` sends the active link, or a new one if the old link expired.
- **Notifications from mybill:** `POST /internal/events` receives `order.paid`, `payment_link.expired` and `payment_link.cancelled`.
  - It requires a Google ID token from mybill-service's runtime account (`MYBILL_EVENTS_SA_EMAIL`, default `276371417285-compute@developer.gserviceaccount.com`), with audience `TASKS_TARGET_URL`.
  - Each event sends **one** WhatsApp message to the customer, deduplicated by `event_id` in `agent.processed_events` (Alembic `0004`). If the send fails, it returns 500 so mybill retries.
  - WhatsApp only allows free-form messages within 24 hours of the customer's last message, so a payment made later than that can't be confirmed on WhatsApp (Meta error 131047, logged).

### Click-to-WhatsApp ad referrals

When a customer taps a Click-to-WhatsApp ad (or an Instagram post's WhatsApp button), her first message carries a `referral`.

- **Stored:** the newest referral on the conversation (`entry_*`) and every one in `agent.conversation_entries` (Alembic 0005). The raw JSON stays in `messages.raw`.
- **Design resolution** (`agent/resolution.py`, before the model; the first hit wins):
  1. mybill marketing link for the ad id (`GET /api/agent/marketing-links/resolve`, cached 10 min, 404s 1 min),
  2. a design code in the ad's body or headline,
  3. a design code in her text.

  The method is saved in `entry_resolution`.
- **First reply** (`agent/ad_entry.py`, no model call): if a design was found and the conversation is new (or quiet for 7+ days), code sends the design's first photo ("<name> – ₹<price> · stitched to your size in N days 🧵") and the buttons 🛍️ Order this / 📸 More photos / 📏 Size guide. A greeting line is added only if the ad had no welcome message.
  - `ad:order` runs start_order, `ad:photos` runs send_design_photos, then the model continues. "Size guide" is answered by the model with get_size_chart.
- **Unresolved ads** go to the model; the facts block says which ad she came from and asks the model to show the latest designs.
- **Events and attribution:**
  - A `conversation_started` event goes to mybill (`POST /api/agent/marketing-events`) without delaying the reply.
  - Orders carry `source_type`, `source_id` and `ctwa_clid` (mybill's Ad performance report).
- **FEP:** `fep_expires_at` = our first reply + 72h, set only for ad entries answered within 24h. Use `agent/fep.py::is_free_template_window` for later reminders.

Test without a real ad (the bot really replies on WhatsApp, so use your own number, listed in `ALLOWED_SENDERS`):

```bash
python scripts/send_fake_referral.py --url http://localhost:8000/webhook/whatsapp \
  --phone 9198XXXXXXXX --ad-id 120210000000000001 --text "Hi! Can I get more info on this?"
# --no-welcome (bot adds a greeting), --no-clid, --media video, --source-type post, --body "New: KUR-101"
```

Map the ad id to a design first in mybill-ui → **Ad & post links**, or put a design code in `--body`.

### Instagram DMs (Phase 9)

Instagram uses the same agent, flow, tools and safety rules as WhatsApp. Everything channel-specific is in `app/channels/`:
- `base.py`: `NormalizedInbound` and the `ChannelAdapter` interface.
- `whatsapp.py`: wraps `whatsapp_client`; payloads unchanged.
- `instagram.py`: parser plus `graph.instagram.com` sending.

The worker's `WorkerMessenger` picks the adapter from `conversation.channel`.

- **Webhook:** `POST /webhook/instagram` checks the signature (`IG_APP_SECRET`, else `WA_APP_SECRET`). It keeps a raw copy (`IG_CAPTURE_RAW`), stores messages, and queues echoes with a 5 s delay.
- **Echoes:** an echo whose mid matches a recorded outbound message is ours. Anything else is staff typing in the Instagram app: it's stored as `author='staff'`, `needs_human` is set, and `STAFF_TOOK_OVER` is logged.
- **Sending:**
  - Text is split at 1000 UTF-8 bytes.
  - Images go without a caption; the caption follows as its own text.
  - Choices are quick replies. If Meta rejects them, numbered text is used instead ("Reply 1, 2 or 3", remembered for 1 h), and a typed number is mapped back via `conversations.last_offered_choices`.
- **Limits:**
  - No reply more than 24 h after the customer's last message.
  - Only `ALLOWED_IG_SENDERS` (Instagram-scoped user ids) get replies while it's set.
- **Customers:** name and username are fetched once from the User Profile API.
- **Instagram is for discovery, WhatsApp for ordering.** On Instagram the agent only gets browsing tools plus `send_whatsapp_order_link`, which sends an "Order on WhatsApp" button: a wa.me link with the message pre-typed, e.g. "Hi SRISA! I'd like to order KUR-101 🛍️ (ref IG-7K3QA)". The `ref_codes` row (Instagram user, design, 7-day expiry) records the handoff.
  - When she sends that message on WhatsApp (`agent/ig_handoff.py` + `worker._instagram_handoff_path`): the ref is marked used, the conversation records `entry_source_type='instagram'`, and mybill gets an upsert with phone + `instagram_user_id`, which merges the two customers.
  - A fresh chat then starts like the ad flow (photo + Order / Photos / Size guide), and the whole order runs on WhatsApp.
- **Token:** `IG_ACCESS_TOKEN` is read from Secret Manager at runtime and cached for 1 h. Locally, use the `IG_ACCESS_TOKEN` env var.
- **Config:** `IG_ACCOUNT_ID` (srisa_fabrics: 17841463885925115).

### Instagram comments → WhatsApp order link

`app/ig_comments.py`. A comment on one of our posts or reels (webhook field `comments`) is stored in `agent.instagram_comments` (Alembic 0010) and processed via the `/tasks/process-comment` Cloud Task.
- **Only buying comments get a reply:** price / order / want / size / details / entha / kitna / ధర / a question mark / a design code.
- **The design** comes from the post's mybill IG_MEDIA link, else `Code: …` in the caption, else a code in the comment.
- **The private reply** (a DM, recipient `comment_id`) contains the design, price, stitching time and a clickable wa.me link with "I'd like to order KUR-101 (ref IG-…)". It's recorded as an outbound message under the conversation lock, so its echo counts as ours.
- **The public reply** is "Thank you 💜 We've sent you the details in a DM. Or WhatsApp us … with code KUR-101".
- **Skipped:** our own comments, other senders while `ALLOWED_IG_SENDERS` is set, and repeats from the same person on the same post within 24 h. Redelivered comments are deduplicated by comment id.

### Staff inbox ("Customer chats" in mybill-ui)

When the AI hands a chat to the team (`request_human_help`, an LLM failure or an order problem), `needs_human_since` is stamped. Every handover except ones staff caused themselves triggers a staff alert (`app/staff_alerts.py`): an approved WhatsApp template `staff_alert` sent to `STAFF_ALERT_NUMBERS`, linking to `MYBILL_UI_URL/?chat=<id>`. Without numbers it's logged only (`STAFF_NEEDED`).

Template to submit in WhatsApp Manager (Utility, English, 4 variables). The same template is used for every staff alert, including chat handovers and measurement calls:
`🔔 SRISA staff alert – {{1}} ({{2}}): {{3}}. Open: {{4}}`

**Exact measurements:** designs flagged in mybill (`exact_measurements`) produce an `order.paid` event with `exact_measurements_required`. The customer's confirmation gets "📏 Our tailor will call you…", and staff get an alert "📏 call for exact measurements – order SB-…" linking to `MYBILL_UI_URL/?order=SB-…`. The alert goes out even if the customer has no WhatsApp chat, and only once per event.

`app/staff_api.py` (`/staff/**`) is authenticated with a Google ID token of mybill-service's service account (or the admin key). mybill-service proxies it as `/api/boutique-chats`. Endpoints:
- the list of waiting chats and a count,
- one conversation with its messages,
- `reply`: sent from the shop's account, recorded as `author='staff'`, bot stays off; refused with 409 `WINDOW_CLOSED` after 24 h,
- `resolve` (AI on again) and `keep` (AI off).

### Tuning the prompt in your terminal

```bash
python scripts/chat_cli.py --mock          # sample catalog, only needs ANTHROPIC_API_KEY
python scripts/chat_cli.py                 # real mybill: MYBILL_BASE_URL + gcloud impersonation of srisa-agent-sa
```

The CLI prints every tool call with its result, the stage after each turn, plus tokens and latency. Buttons and lists are shown with their ids; tap them with `/tap <id>`, e.g. `/tap size:M` or `/tap confirm:<draft>:<version>`. With `--mock`, orders are fake. Without it, the CLI creates **real** customers and orders in mybill. `system.md` is re-read on every turn, so edit it while you chat. Commands: `/tap <id>`, `/image [caption]`, `/voice`, `/progress`, `/facts`, `/reset`, `/quit`.

## 1. Local setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env    # fill in WA_* and the Neon URLs (from Neon console → Connect)
```

Keep `USE_CLOUD_TASKS=false` locally. Messages are then processed in a FastAPI BackgroundTask, so no Cloud Tasks is needed.

## 2. Migrations

From your Mac against Neon (uses `DATABASE_URL_DIRECT` from `.env`):

```bash
alembic upgrade head        # creates schema "agent" and the tables
alembic current             # shows 0001 (head)
```

Optional local Postgres instead of Neon:

```bash
docker compose up -d db
DATABASE_URL=postgresql://agent:agent@localhost:5433/agent DATABASE_URL_DIRECT= alembic upgrade head
```

New migration later: `alembic revision -m "describe change"`, then edit the file. Always pass `schema=SCHEMA` to `op.*` calls.

## 3. Run locally

```bash
uvicorn app.main:app --reload --port 8080
curl http://localhost:8080/health
curl "http://localhost:8080/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=XXX&hub.challenge=12345"
```

Send a fake signed webhook. This uses the `WA_APP_SECRET` from `.env`:

```bash
set -a; source .env; set +a
BODY=$(cat tests/fixtures/text_message.json)
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WA_APP_SECRET" | sed 's/^.* //')
curl -i -X POST http://localhost:8080/webhook/whatsapp \
  -H "Content-Type: application/json" -H "X-Hub-Signature-256: sha256=$SIG" --data-binary "$BODY"
```

For real WhatsApp traffic, run `cloudflared tunnel --url http://localhost:8080` and set the tunnel URL as the Meta Callback URL.

## 4. Tests

The tests use a real Postgres, because they need advisory locks and jsonb. They create a throwaway schema **`agent_test`**, migrate it, truncate it before each DB test and drop it at the end. They never touch `agent` or any other schema. The Graph API is mocked with respx.

```bash
pytest                                   # uses DATABASE_URL_DIRECT from .env (Neon)
```

From India each Neon query takes about 0.5 s, so the full suite takes several minutes. For a fast run, use the local Postgres:

```bash
docker compose up -d db
TEST_DATABASE_URL=postgresql://agent:agent@localhost:5433/agent pytest
```

## 5. Google Cloud setup (one time)

Run these yourself, in order.

```bash
PROJECT=project-0708fb26-e204-4400-ae9
PROJECT_NUMBER=276371417285
REGION=asia-south1
RUNTIME_SA=srisa-agent-sa@$PROJECT.iam.gserviceaccount.com
TASKS_SA=srisa-agent-tasks@$PROJECT.iam.gserviceaccount.com
DEPLOY_SA=github-actions@$PROJECT.iam.gserviceaccount.com
gcloud config set project $PROJECT

# APIs
gcloud services enable run.googleapis.com cloudtasks.googleapis.com secretmanager.googleapis.com

# Service accounts: one the service runs as, one Cloud Tasks signs its OIDC tokens with
gcloud iam service-accounts create srisa-agent-sa --display-name="srisa-agent runtime"
gcloud iam service-accounts create srisa-agent-tasks --display-name="srisa-agent Cloud Tasks invoker"

# Queue: same region as the service; max 5 attempts with exponential backoff
gcloud tasks queues create srisa-agent-messages --location=$REGION \
  --max-attempts=5 --min-backoff=5s --max-backoff=300s --max-doublings=4 \
  --max-concurrent-dispatches=10
# (to change later: gcloud tasks queues update srisa-agent-messages --location=$REGION --max-attempts=5 ...)

# Runtime SA may create tasks, and may act as the tasks SA (to attach its OIDC token)
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$RUNTIME_SA" --role="roles/cloudtasks.enqueuer"
gcloud iam service-accounts add-iam-policy-binding $TASKS_SA \
  --member="serviceAccount:$RUNTIME_SA" --role="roles/iam.serviceAccountUser"

# GitHub deploy SA may deploy services that run as the runtime SA
gcloud iam service-accounts add-iam-policy-binding $RUNTIME_SA \
  --member="serviceAccount:$DEPLOY_SA" --role="roles/iam.serviceAccountUser"

# Let GitHub Actions in this repo use the deploy SA (same pattern as mybill-ecom)
gcloud iam service-accounts add-iam-policy-binding $DEPLOY_SA \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/vrajaramesh/srisa-agent"
```

**Secrets.** Paste each value when prompted, then press Ctrl-D. This keeps the values out of your shell history. `DATABASE_URL` is the Neon **pooled** URL.

```bash
for S in WA_ACCESS_TOKEN WA_APP_SECRET WA_VERIFY_TOKEN DATABASE_URL ALLOWED_SENDERS ANTHROPIC_API_KEY ADMIN_API_KEY; do
  echo "Value for $S (then Ctrl-D):"
  tr -d '\n' | gcloud secrets create $S --data-file=- --replication-policy=automatic
  gcloud secrets add-iam-policy-binding $S \
    --member="serviceAccount:$RUNTIME_SA" --role="roles/secretmanager.secretAccessor"
done
# Update one later: printf '%s' 'NEW_VALUE' | gcloud secrets versions add DATABASE_URL --data-file=-
```

`ALLOWED_SENDERS` is a secret, not a plain env var, because this repo is public and it contains a phone number. When the bot should reply to everyone, set it to an empty value.

`ADMIN_API_KEY` can be any long random string, e.g. from `openssl rand -hex 32`.

mybill-service only trusts `srisa-agent-sa`, so the runtime service account needs no extra role to call it. Cloud Run's metadata server issues the ID token.

**GitHub repo secrets** (Settings → Secrets and variables → Actions): `WIF_PROVIDER` and `WIF_SERVICE_ACCOUNT`, with the same values as in the mybill-ecom repo.

**After the first deploy,** allow the tasks SA to call the service:

```bash
gcloud run services add-iam-policy-binding srisa-agent --region=$REGION \
  --member="serviceAccount:$TASKS_SA" --role="roles/run.invoker"
```

The service is public (`--allow-unauthenticated`) because Meta can't send Google credentials. The worker endpoint is protected in the app instead: it requires a Google-signed OIDC token for `$TASKS_SA` with audience = the service URL.

## 6. Deploy

Pushing to `main` runs `.github/workflows/deploy.yml`. It builds the image, pushes it to Artifact Registry (`us-central1-docker.pkg.dev/$PROJECT/mybill/srisa-agent`) and deploys to Cloud Run in `asia-south1` with `--min-instances=0`.

Equivalent manual deploy (Apple Silicon Mac → build for amd64):

```bash
IMAGE=us-central1-docker.pkg.dev/$PROJECT/mybill/srisa-agent:manual
docker build --platform linux/amd64 -t $IMAGE . && docker push $IMAGE
gcloud run deploy srisa-agent --image=$IMAGE --region=$REGION \
  --service-account=$RUNTIME_SA --allow-unauthenticated \
  --min-instances=0 --max-instances=3 --memory=512Mi --cpu=1 --timeout=60 \
  --set-env-vars=GRAPH_API_VERSION=v25.0,LOG_LEVEL=INFO,WA_PHONE_NUMBER_ID=1369784996225320,DB_SCHEMA=agent,USE_CLOUD_TASKS=true,GCP_PROJECT=$PROJECT,TASKS_QUEUE=srisa-agent-messages,TASKS_LOCATION=$REGION,TASKS_TARGET_URL=https://srisa-agent-$PROJECT_NUMBER.$REGION.run.app,TASKS_SERVICE_ACCOUNT=$TASKS_SA,LLM_MODEL=claude-sonnet-5-5,MYBILL_BASE_URL=https://mybill-service-$PROJECT_NUMBER.us-central1.run.app,MYBILL_AUDIENCE=https://mybill-service-$PROJECT_NUMBER.us-central1.run.app,ADMIN_ALLOWED_EMAILS=srisafabricshyd@gmail.com \
  --set-secrets=WA_ACCESS_TOKEN=WA_ACCESS_TOKEN:latest,WA_APP_SECRET=WA_APP_SECRET:latest,WA_VERIFY_TOKEN=WA_VERIFY_TOKEN:latest,DATABASE_URL=DATABASE_URL:latest,ALLOWED_SENDERS=ALLOWED_SENDERS:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,ADMIN_API_KEY=ADMIN_API_KEY:latest
```

Then set the Meta **Callback URL** to `https://srisa-agent-276371417285.asia-south1.run.app/webhook/whatsapp`, using the same verify token.

**Order for the first rollout:**
1. `alembic upgrade head` (applies up to `0003`: `order_drafts`, stage values, `messages.reply_id`, `conversations.mybill_customer_id`)
2. GCP setup (section 5)
3. Push to `main`
4. Grant `run.invoker`
5. Switch the Meta Callback URL

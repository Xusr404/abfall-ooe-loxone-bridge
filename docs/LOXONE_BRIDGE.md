# Loxone setup and API reference

Install and start the bridge using the [README](../README.md#choose-an-installation). For server settings, updates, and removal, see the [Linux installation guide](INSTALLATION.md) or [Docker guide](DOCKER.md).

## Set up Loxone

1. Open `http://BRIDGE_HOST:8765` from your home network, using your configured host and port.
2. Use **Adressen aktualisieren** if the address list is missing, then choose the district, municipality, street or locality, and house number. Selecting a street looks up its house numbers through the bridge.
3. Enter the bridge host and port as reachable from the Miniserver. If the bridge requires an API key, enter the same key here.
4. Select **XML herunterladen**. The bridge chooses the default interval for each waste type.
5. In Loxone Config, import the file through the templates for virtual HTTP inputs, check its address, and transfer the configuration to the Miniserver.

The Windows launcher listens only on that PC. A separate Miniserver needs a bridge reachable over your home network, such as the Linux or Docker installation.

You can also copy the generated pickup URL for your own integration. For dynamic values in PicoC, use `httpget(address, page)` with the bridge host and request path.

The setup page stores the selected address, host, and port locally in the browser. API keys are not saved in browser storage, but are included in the generated XML and pickup URL when supplied.

## Endpoints and authentication

| Endpoint | Purpose | API key |
| --- | --- | --- |
| `/` or `/setup/` | Address selection and Loxone configuration | Not required |
| `/health` | Service status | Not required |
| `/api/v1/pickups` | Cached pickup dates for an address | Required when configured |
| `/api/v1/logs?lines=100` | Recent bridge log lines | Required when configured |

When a key is configured, supply it as `key=...` in the query string or in the `X-Api-Key` request header. The key protects pickup and log requests; the setup page and address lookups remain accessible without it. Starting a full address refresh and loading calendar interval choices require the configured key. Keep the bridge on a trusted home network.

`/health` returns `{"status":"ok"}` when the service responds. It does not contact Umweltprofis or check your Loxone configuration.

## Pickup request

The setup page generates a URL with all four address **names**. `city_name` is the municipality:

```text
http://BRIDGE_HOST:8765/api/v1/pickups?district_name=Testbezirk&city_name=Testgemeinde&street_name=Teststra%C3%9Fe%20S%C3%BCd_1&house_number_name=1%2F2
```

This invented example preserves the street name `Teststraße Süd_1` and house number `1/2`.
Use standard UTF-8 query encoding (`URLSearchParams` or Python's `urllib.parse.urlencode`).
Spaces may be `%20` or `+`; literal plus signs must be `%2B`. Underscores stay underscores.
Only surrounding whitespace and canonical Unicode (NFC) are normalized. Matching is case-sensitive
and exact within each level of the district → municipality → street → house-number hierarchy.
Missing names return `422 address_not_found`; duplicate matches return `422 address_ambiguous`
with the affected `field`. Incomplete input returns `400`. ID parameters are rejected.
Add the configured key if required.

Add `details=1` to include the address, pickup rows, and up to three upcoming dates per waste type. The default response includes the next date, days remaining, and a summary by waste type.

### Example response

This is an excerpt with invented dates and waste types, not data fetched from Umweltprofis:

```json
{
  "ok": true,
  "status": "ok",
  "cache_stale": false,
  "refresh_error": null,
  "next_pickup": {
    "date": "2026-11-05",
    "days_until": 1,
    "waste_types": ["Bio", "Restabfall"]
  },
  "by_waste_type": {
    "Bio": {"date": "2026-11-05", "days_until": 1},
    "Restabfall": {"date": "2026-11-05", "days_until": 1}
  }
}
```

Responses also include `fetched_at` and `cache_age_seconds`. A collection on the current Vienna calendar day has `days_until: 0`. Empty results distinguish `no_pickups` from `no_upcoming_pickups` when previously listed collections have passed.

## Intervals and cache behavior

The pickup endpoint filters by the first interval offered by the calendar for each waste type, then removes duplicate waste-type/date combinations. Requests can override this default with the optional `intervals` parameter. The stored cache retains up to three future dates per waste type and interval.

A successful calendar fetch is reused for the current Europe/Vienna day. Every new fetch resolves
the entire address hierarchy with current upstream IDs, using the same upstream session for all
lookups and calendar selection. A possible stale-ID/connection failure triggers one fresh resolution
and retry after a short backoff. The entire individual operation, including queue time and retry,
has a 20-second budget. Failed attempts have a persisted 60-second retry delay, rather than a daily lockout.

Saved calendars are keyed by names, so changed IDs do not remove their dates. A failed fetch, or an
empty replacement while future saved dates remain, preserves the previous calendar and last successful
timestamp. Usable saved dates return `cache_stale: true`, `cache_status: "stale"`,
`last_successful_fetch_at` (also `fetched_at`), and `refresh_error`. Expired dates are removed;
a stale calendar with no future dates for the selected interval is never reported as current.
Without usable saved dates, temporary failures return HTTP `503` with `temporary: true`,
`retry_after_seconds`, `retry_at` (UTC), and a `Retry-After` header. Retry at or after that time.
Successful fetches with no collections return `no_pickups` or `no_upcoming_pickups`.

Concurrent requests for one address share one in-flight operation, even if a caller disconnects.
There are four active individual upstream operations and at most sixteen queued different addresses
(shared by calendar and helper lookups). Queue deadlines return `lookup_timeout`; overload returns
`503 lookup_overloaded` with a five-second retry suggestion. Current cached calendars remain available
without entering the queue. A full refresh uses a separate upstream session and does not occupy these slots.

The calendar website can change independently of this project. Automated tests use synthetic data and do not check the live website.

## Troubleshooting requests and setup

| Symptom | What to check |
| --- | --- |
| Setup page does not open | Confirm the host, configured port, and whether the service is running. The Windows launcher accepts connections only from that PC. |
| Address list is missing | Use **Adressen aktualisieren** on the setup page to start the download. |
| Pickup or log request returns `401` | Configure the same API key on the bridge and in your request or generated XML. |
| Pickup data is stale or returns `503` | Check the logs and observe `Retry-After`. Individual recovery does not wait for a full address download. |
| Address request returns `400` or `422` | Check that the address is complete and its names are exact and unambiguous; regenerate the URL through setup. |

For Linux service status and logs, see [installation troubleshooting](INSTALLATION.md#troubleshooting). For containers, see [Docker troubleshooting](DOCKER.md#troubleshooting).

## Saved data

For source launches, cache and rotating logs live under `bridge/data/`; the address snapshot lives under `tools/helpers/data/`. These generated directories are ignored by Git.

The Linux installer uses `/var/lib/loxone-bridge/` for cache and addresses and `/var/log/loxone-bridge/` for logs. Docker stores these in named volumes as described in its guide.

`--cache-file` and `--log-file` select alternate runtime locations. `LOXONE_BRIDGE_ADDRESS_DATA_FILE` selects the address snapshot for both the helper and refresh utility.

## Inactive calendar cleanup

The bridge tracks the last calendar request for each saved address, including requests served
from today's cache and requests to load interval choices. Address identity and calendar refresh
rules stay the same: successful calendars are cached for the Vienna day, with the existing bounded
recovery and retry delay after failures. Retention does not add a refresh rule based on the last pickup.

An address becomes eligible for deletion after **91 complete days** without a request: 90 days of
inactivity plus a one-day grace period for activity lost in a crash. Cleanup checks every 24 hours,
so deletion happens at the next successful check after that threshold. Its calendar and associated
per-address aliases, attempt/error/retry metadata and activity record are removed together.
Lookups in progress are skipped. District, municipality and street snapshots are managed separately
and are never removed by this cleanup.

Activity lives in a separate file beside the calendar cache. With the default cache filename this is
`loxone_bridge_cache.activity.json`; with `--cache-file /path/custom.json` it is
`/path/custom.activity.json`. Keep this file with the cache when backing up or moving saved state.
It stores UTC timestamps for `last_requested` and `last_cleanup_at`. Requests update activity only in
memory. The daily cleanup check atomically saves activity and its completion marker; there are no
per-request or shutdown activity writes. A daily completion marker is saved even when no addresses
were removed, so restarts can preserve the schedule. Requests arriving during activity persistence
stay dirty in memory for the following daily save; the grace day covers that unpersisted activity.

On startup, a valid check marker less than 24 hours old schedules the remaining interval without
rewriting either file. A due check runs immediately, including after several stopped days. Restarting
does not move that deadline. Missing, malformed, future, timezone-free or unreadable activity data is
logged and affected saved addresses receive a safe baseline of the current time, rather than being
assumed inactive. An invalid check marker also resets saved-address baselines before repairing the
schedule. Failed checks retain the previous successful marker, log the failure, and retry after five
minutes while the application runs. Failure backoff is in memory; a restarted service retries a check
that is still due. Repeated successful restarts within 24 hours do not perform another check.

Past pickup dates are filtered in memory on reads and startup. They are removed from disk only when
another operation already requires a calendar-cache write. A check with no deletions does not rewrite
the calendar cache. Atomic publication uses the shared persistence lock and revalidates deletions and
calendar changes before the final rename, so requests arriving during file preparation can rescue an
address and newer calendar updates are preserved. If recording the completion marker fails after a
calendar deletion was published, the next check safely repeats the remaining bookkeeping.

Cleanup is owned by the application's startup/shutdown lifecycle and has one scheduled task per
bridge. This coordination, like lookup sharing, assumes **one server process per data directory**.
It does not provide coordination across multiple workers or replicas.

## Schedule selection and migration

**Breaking change:** regenerate old ID-based URLs/XML using setup. The API accepts names only.
The bridge automatically chooses the first interval offered by the calendar for each waste type.
For manual overrides, `intervals` remains an optional URL-encoded JSON object mapping waste-type
**names** to interval **names**, for example `{"Restabfall":"4-wöchentlich"}` before URL encoding.
Matching uses the same trim/NFC rules as addresses; missing or ambiguous interval names return
`422 interval_not_found` / `interval_ambiguous`.
Malformed JSON, non-string values, more than 64 selections, or names longer than 128 characters return `400`.
No interval or waste-type IDs are needed, including after an upstream renumbering.

`GET /setup/api/v1/calendar-intervals` accepts the same address names and requires the configured key.
It returns `choices` (waste-type names mapped to arrays of interval names), `cache_stale`,
`last_successful_fetch_at`, and `refresh_error`, using the same calendar cache as pickups.
The helper's `/setup/api/v1/locations/{cities,streets,house-numbers,intervals}` endpoints accept all
ancestor names. House numbers always resolve current district, municipality and street IDs;
they never trust the helper snapshot's saved IDs. Interval lookups also resolve the house number.

Setup saves the selected address in browser storage. It can restore an old browser selection if its
IDs still exist in the saved snapshot; after renumbering, select names again if needed.
A legacy ID-keyed calendar can be carried forward when a saved name alias links it to the requested
names. Legacy calendars without that link are retained on disk, but cannot safely be attributed to
an address after IDs change. The bridge does not guess that association.

## Refresh access and cooldown

`POST /setup/api/v1/address-data/refresh` requires `X-Refresh-Request: 1` and the configured key.
Setup sends the key as `X-Api-Key`; Unicode keys use percent-encoded UTF-8 with
`X-Api-Key-Encoding: percent`. Other clients may continue using the existing plain header or
URL-encoded `key` query parameter. Control characters are rejected in configured keys.

Full address downloads have a persisted one-hour cooldown from the start of each attempt, even
if it fails or the service restarts. Duplicate requests during a running download return its state.
Later requests during cooldown return `429` with seconds in `Retry-After`. An unreadable cooldown
file returns `503`; inspect its permissions and contents rather than repeatedly retrying downloads.
The marker is stored beside the address snapshot as `address-refresh-attempt.json`.
The snapshot, ordinary address lookups, and refresh-status polling remain accessible without a key.

Possible stale-ID failures also schedule this same full refresh in the background; the same
single-flight task and one-hour cooldown apply to automatic and manual requests. A full crawl has
a 15-minute deadline. Only a completely built hierarchy with nonempty branches, valid unique IDs
within each parent, and a valid timestamp is atomically published. Any failed or invalid crawl
leaves the last snapshot in place. The validator conservatively rejects empty municipality/street
branches; if Umweltprofis legitimately introduces one, this rule needs adjustment.

Snapshot publication and live per-address calendar/mapping updates have separate ownership. A full
crawl never writes per-address mappings or dates, and individual recovery never patches a snapshot.
This prevents either completion order from overwriting the other result. Individual requests always
resolve live IDs before fetching a new calendar, independently of the snapshot's age.

**Deployment constraint: one server process per cache/address-data directory.** In-flight sharing,
queue admission and refresh exclusion operate in that process. Do not run multiple workers, replicas,
the standalone helper, or the CLI refresh utility concurrently against the same data directory.
Stop the service before using the CLI utility. Multi-process deployments require an external lock
and shared job/cache store; they are not supported by this implementation.

Generated URLs and XML contain the key. HTTP transmits it unencrypted. Do not share these files,
URLs, browser histories, or proxy logs. Keep the service on a trusted network.

## Adapting the XML

The template retains commands for `Bioabfall`, `Altpapier`, `Restabfall`, and `Gelber Sack`.
If your calendar uses different names, edit each command's title and the waste-name literal in its
`Check` expression to match the JSON `by_waste_type` key exactly. Duplicate a `VirtualInHttpCmd`
element for additional types and adjust both fields. Missing types have no `days_until` reading;
do not assume the Miniserver clears an earlier value. Test this and stale-data behavior on your hardware.
The template does not provide a separate stale-data alarm; configure one if required.

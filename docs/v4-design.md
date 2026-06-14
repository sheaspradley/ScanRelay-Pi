# ScanRelay v4.0 — Incident-aware
## Summary
ScanRelay v4.0 changes ScanRelay from a keyword-triggered relay into an incident-aware scanner brain. Instead of treating every transmission as a standalone alert, v4 listens continuously, transcribes in near real time, extracts structured event data, clusters related transmissions into ongoing incidents, and updates the user through Meshtastic, ntfy, and the dashboard. The core intelligence remains local: transcription, extraction, clustering, prioritization, and summarization run on the Raspberry Pi without cloud model APIs or hosted incident services.

---
## Motivation
### What v3 lacks
ScanRelay v3 is useful because it is simple: capture scanner audio, transcribe it, match user-configured keywords, format a message, and relay it. That model works well when the user wants to know that an interesting phrase was heard. It works less well when a real incident unfolds across many short transmissions.

Radio traffic is conversational. A single call can include dispatch, acknowledgment, response, arrival, size-up, resource requests, command updates, containment, and clearing. A keyword matcher sees those as separate events. A human listener understands them as one developing incident.

### Example scenario
A generic structure-fire call might sound like this:

| Time | Speaker | Transmission |
| --- | --- | --- |
| 13:42:18 | Dispatch | "Engine 41, structure fire, 1500 California Street, smoke showing" |
| 13:42:35 | Engine 41 | "Engine 41 en route" |
| 13:48:02 | Engine 41 | "Engine 41 on scene, working fire, requesting additional units" |
| 13:49:14 | Dispatch | "Engine 48, Truck 12, respond to 1500 California with Engine 41" |
| 13:55:30 | Engine 48 | "Engine 48 on scene" |
| 14:03:45 | Engine 41 | "Engine 41 has command, fire is contained to one room" |
| 14:18:22 | Engine 41 | "Engine 41 fire out, units returning" |

A v3-style flow is likely to produce seven separate alerts. A v4 flow should create one incident notification and update it as the incident develops:

- `Structure fire — 1500 California Street — smoke showing`
- `Update: working fire; additional units requested`
- `Update: command established; contained to one room`
- `Resolved: fire out; units returning`

The product shift is not only fewer notifications. The shift is continuity: ScanRelay should understand that the later traffic belongs to the earlier dispatch.

### Why now
The Raspberry Pi 5 with 16 GB RAM has enough headroom to run audio capture, streaming `whisper.cpp`, a small local LLM, SQLite, delivery workers, and a dashboard. Small models such as Llama 3.2 3B, Phi-3 Mini, and Qwen 2.5 3B are plausible for constrained extraction and scoring. ScanRelay already has the v3 foundation for capture, filtering, delivery, and dashboard history, so v4 can evolve the existing daemon rather than replace it wholesale.

---
## Goals & non-goals
### Goals
1. **Reduce alert noise for multi-transmission incidents.** Related transmissions should attach to an incident, and the phone should update only for meaningful developments.
2. **Preserve raw evidence.** Every stored event keeps the raw transcript. Model-derived fields add structure but never replace the transcript.
3. **Keep intelligence local.** v4 does not require cloud transcription, hosted LLM APIs, or external incident-processing services.
4. **Improve latency.** Streaming transcription should surface useful words within roughly 1-2 seconds under normal conditions.
5. **Make incidents inspectable.** The dashboard should show active and cleared incidents, linked events, timelines, units, status, confidence, and delivery history.
6. **Degrade safely.** If transcription, LLM extraction, clustering, or delivery fails, ScanRelay should keep storing events and fall back toward v3-style keyword behavior.
7. **Remain configurable.** Users should be able to tune model choice, clustering thresholds, retention, and alert behavior without changing code.

### Non-goals
1. **No cloud dependency for the v4 brain.** Optional future integrations are separate from the core design.
2. **No claim of official incident truth.** Extracted incident data is best-effort interpretation of scanner audio, not an official dispatch record.
3. **No automated emergency response.** ScanRelay relays and summarizes; it does not dispatch resources or provide operational guidance.
4. **No public incident map by default.** Local dashboard display is in scope; sharing maps or feeds is opt-in future work.
5. **No biometric speaker identity.** Diarization should infer roles or labels where useful, not identify people.
6. **No removal of keyword mode.** Keyword matching remains a deterministic signal and fallback path.
7. **No full CAD replacement.** v4 models enough incident state to make alerts clearer, not to reproduce an agency dispatch system.

---
## Architecture
### High-level diagram
```text
+-------------------+
| Police scanner    |
+---------+---------+
          |
          v
+-------------------+      +-------------------+
| USB audio capture | ---> | Audio ring buffer |
+---------+---------+      +-------------------+
          |
          v
+-------------------+
| VAD / squelch gate|
+---------+---------+
          |
          v
+-------------------------------+
| whisper.cpp server mode       |
| streaming partials + finals   |
+---------------+---------------+
                |
                v
+-------------------------------+
| Event builder                 |
| transcript + timestamps       |
+---------------+---------------+
                |
                v
+-------------------------------+
| Local LLM brain               |
| extraction + scoring hints    |
+---------------+---------------+
                |
                v
+-------------------------------+
| Structured extractor          |
| units/type/address/status     |
+---------------+---------------+
                |
                v
+-------------------------------+
| Incident clusterer            |
| time/address/unit/semantic    |
+-------+---------------+-------+
        |               |
        v               v
+---------------+   +------------------+
| Alert delivery|   | SQLite database  |
| mesh + ntfy   |   | incidents/events |
+-------+-------+   +---------+--------+
        |                     |
        v                     v
+---------------+   +------------------+
| Mesh radio    |   | Dashboard        |
| Phone push    |   | incident view    |
+---------------+   +------------------+
```

### Runtime processes
- `scanrelay-daemon`: owns capture orchestration, event creation, clustering, and delivery.
- `whisper.cpp` server: long-lived streaming transcription service.
- Local LLM runtime: local model server, binding, or subprocess wrapper for extraction and scoring.
- `scanrelay-dashboard`: local web UI for incidents, events, and diagnostics.
- systemd: starts, restarts, and health-checks long-running services.

### Control flow
1. Scanner audio enters through the configured USB device.
2. Audio capture writes frames into the VAD and optional ring buffer.
3. VAD suppresses silence and helps determine segment boundaries.
4. Audio frames stream into `whisper.cpp` server mode.
5. Partial transcripts may be shown for low-latency preview.
6. Final transcripts become durable `Event` candidates.
7. The LLM extracts units, incident type, address, status, and confidence.
8. The clusterer compares the event with active and recently resolved incidents.
9. The database stores the event, incident state, and clustering evidence.
10. Delivery decides whether to create a notification, update an incident thread, send mesh text, or store silently.
11. The dashboard renders incident lists, timelines, raw transcripts, and extracted fields.

### Failure boundaries
- If streaming transcription fails, restart it and optionally fall back to batch transcription.
- If LLM extraction fails, store the raw transcript and use deterministic keyword behavior.
- If clustering is uncertain, keep the event standalone or mark it as a candidate rather than forcing a merge.
- If ntfy delivery fails, record the attempt and keep mesh delivery independent.
- If the dashboard fails, the daemon should continue capturing and storing events.

---
## Data model
v4 keeps `Event` as the durable record of a heard transmission and adds `Incident` as the durable record of a developing situation.

### Event
An `Event` is one finalized transmission or transcription segment. It may be linked to an incident or remain standalone.

```json
{
  "id": "evt_2026_001234",
  "created_at": "2026-01-15T13:42:18Z",
  "audio_started_at": "2026-01-15T13:42:16Z",
  "audio_ended_at": "2026-01-15T13:42:22Z",
  "transcript": "Engine 41, structure fire, 1500 California Street, smoke showing",
  "transcript_confidence": 0.86,
  "speaker": {
    "role": "dispatcher",
    "label": "Dispatch",
    "confidence": 0.74
  },
  "extracted": {
    "units": ["Engine 41"],
    "incident_type": "structure_fire",
    "address": {
      "raw": "1500 California Street",
      "normalized": "1500 California Street",
      "confidence": 0.81
    },
    "status": "dispatched",
    "confidence": 0.82
  },
  "incident_id": "inc_2026_000042"
}
```

Required event properties:

- stable event id;
- capture and creation timestamps;
- raw transcript;
- optional transcript confidence;
- optional speaker role and label;
- extracted structured fields;
- nullable incident id;
- delivery metadata.

### Incident
An `Incident` is a cluster of related events with a current state, summary, location, units, and notification thread.

```json
{
  "id": "inc_2026_000042",
  "created_at": "2026-01-15T13:42:18Z",
  "updated_at": "2026-01-15T14:18:22Z",
  "state": "resolved",
  "incident_type": "structure_fire",
  "title": "Structure fire — 1500 California Street",
  "summary": "Fire reported with smoke showing; command later reported fire out and units returning.",
  "address": {
    "raw": "1500 California Street",
    "normalized": "1500 California Street",
    "lat": null,
    "lon": null,
    "confidence": 0.81
  },
  "units": [
    {
      "label": "Engine 41",
      "role": "primary_unit",
      "status": "returning",
      "confidence": 0.87
    }
  ],
  "status": {
    "current": "fire_out",
    "history": ["dispatched", "en_route", "on_scene", "working", "contained", "fire_out"]
  },
  "confidence": {
    "type": 0.86,
    "address": 0.81,
    "cluster": 0.91,
    "overall": 0.86
  }
}
```

Required incident properties:

- stable incident id;
- first and last event timestamps;
- lifecycle state;
- normalized incident type;
- display title and summary;
- best known address;
- participating units;
- current status and status history;
- confidence values;
- linked event ids;
- notification thread metadata.

### Unit
A `Unit` is a normalized participant label extracted from text, context, or configuration.

```json
{
  "id": "unit_engine_41",
  "label": "Engine 41",
  "aliases": ["E41", "Engine Forty One"],
  "type": "fire_engine",
  "status": "on_scene",
  "confidence": 0.84,
  "source": "transcript"
}
```

Unit normalization should handle aliases, transcript variation, and confidence. Unknown units should be stored as uncertain rather than discarded.

### Address
An `Address` is a location phrase extracted from a transmission. It may be exact, partial, landmark-like, cross-street-like, or unknown.

```json
{
  "raw": "1500 California Street",
  "normalized": "1500 California Street",
  "kind": "street_address",
  "lat": null,
  "lon": null,
  "confidence": 0.81,
  "privacy_level": "local_only"
}
```

Coordinates should be nullable. Geocoding should not be required for clustering and should not call external services by default.

### Lifecycle states
- `new`: incident just created from an initial event.
- `active`: incident is ongoing and can receive updates.
- `updating`: transient state while a meaningful update is being processed and delivered.
- `resolved`: closure language or timeout indicates the incident is likely over.
- `cleared`: incident is no longer eligible for normal automatic attachment.

```text
new -> active -> updating -> active
                  |
                  v
              resolved -> cleared
```

Rules:

- `new` becomes `active` after initial classification.
- `active` becomes `updating` when a meaningful change arrives.
- `updating` returns to `active` after notification state is synchronized.
- `active` becomes `resolved` when closure language is detected.
- `resolved` becomes `cleared` after a configurable quiet period.
- `active` can become `cleared` after a long timeout.
- `resolved` can reopen only with strong evidence.

---
## Components
### Streaming transcription (`whisper.cpp` server mode)
v4 should replace one-shot `whisper-cli` invocation with a long-lived `whisper.cpp` server. The daemon streams audio into the server and receives partial and finalized text. Final text creates events; partial text is optional UI and metrics data.

Design requirements:

- support partial transcript events;
- support finalized transcript events;
- retain batch fallback mode;
- measure latency at each stage;
- restart or reconnect when the server fails;
- keep raw transcript text unchanged after finalization;
- expose health status to logs and dashboard diagnostics.

Segment boundaries should combine VAD, silence thresholds, maximum segment length, and server finalization. A short audio pre-roll can reduce clipped first words. The event builder should normalize whitespace and obvious decoder artifacts but must not invent units, addresses, or status.

Useful metrics:

- audio start to first partial token;
- audio end to final transcript;
- final transcript to extraction complete;
- extraction complete to delivery attempted;
- total time from speech start to phone push attempt.

### LLM brain
The local LLM is the shared reasoning engine for extraction, semantic similarity, status interpretation, prioritization hints, and short summaries. It should be constrained by deterministic validation and should never be the only safeguard.

Candidate models:

| Model | Strengths | Tradeoffs | Likely role |
| --- | --- | --- | --- |
| Llama 3.2 3B | strong instruction following and broad ecosystem | may be slower depending on quantization | default if benchmarks are good |
| Phi-3 Mini | compact and strong for size | packaging and license review required | alternate default candidate |
| Qwen 2.5 3B | good structured-output behavior | runtime benchmarking required | strong extraction candidate |

The model interface should support health checks, model metadata, prompt execution, timeouts, structured output validation, latency metrics, and fallback responses. The runtime can be `llama.cpp` server, a Python binding, a local HTTP service, or a supervised subprocess.

Prompt design should be conservative:

- include only the transcript and relevant recent context;
- provide allowed incident types and status values;
- include known unit aliases if configured;
- instruct the model to use `unknown` or `null` when evidence is missing;
- ask for confidence per field;
- require short explanations for clustering evidence;
- keep temperature low;
- validate output before storage.

Validation should reject unparseable output, missing required keys, invalid enums, out-of-range confidence, unsupported summaries, addresses not grounded in transcript/context, and unit labels that conflict with known aliases without enough evidence.

### Structured event extraction
The extractor converts a transcript into fields like:

```json
{
  "units": ["Engine 41"],
  "incident_type": "structure_fire",
  "address": "1500 California Street",
  "status": "dispatched",
  "confidence": 0.82
}
```

Extraction should add structure but keep the raw transcript visible. Low-confidence fields should remain unknown. For example, a short acknowledgment may identify a unit and status but not an address or incident type. That is acceptable because clustering can use context from earlier events.

The extractor should output both normalized values and raw evidence where possible. For example, `address.raw` preserves the phrase heard in the transcript, while `address.normalized` is used for display and matching.

### Incident clusterer
The clusterer is the headline feature. It decides whether a finalized event starts a new incident, updates an existing incident, reopens a resolved incident, remains standalone, or becomes a candidate link for later review.

Inputs:

- event timestamp;
- raw transcript;
- extracted units;
- extracted incident type;
- extracted address;
- extracted status;
- speaker role;
- keyword metadata;
- active incidents;
- recently resolved incidents;
- configured thresholds;
- known unit aliases;
- optional semantic similarity score.

Signals for "same incident":

1. **Time window.** Events within 0-20 minutes of the last incident event are strong candidates; 20-60 minutes is weaker; 60-120 minutes should require strong address, unit, or incident-type evidence.
2. **Address match.** Exact normalized address is strong. Same block, street, landmark, or cross-street can be useful but should be tempered by time and type.
3. **Unit overlap.** The same unit responding, arriving, taking command, or clearing is strong continuity evidence.
4. **Incident type compatibility.** Some changes are natural upgrades, such as investigation to working incident or alarm to false alarm. Incompatible types reduce confidence.
5. **Status progression.** Dispatched → en route → on scene → working → contained → resolved is more plausible than a random sequence.
6. **Semantic similarity.** The LLM can compare a new event to active incident summaries when address or unit evidence is missing.
7. **Conflict penalties.** Different units, incompatible types, corrected addresses, or low transcript confidence should reduce the score.

Scoring sketch:

```text
score = time + address + unit_overlap + type_compatibility + status_progression + semantic_similarity - conflicts
if score >= attach_threshold: attach to incident
elif score >= review_threshold: keep as candidate
else: create new incident or leave standalone
```

This is pseudocode only. Exact weights should be tuned with synthetic and real local evaluation data.

Cluster confidence bands:

- `high`: attach automatically and update incident state;
- `medium`: attach only if deterministic evidence is strong, otherwise keep as candidate;
- `low`: do not attach;
- `conflict`: create a separate incident or leave standalone.

Meaningful updates include escalation, new address, corrected type, additional units, arrival, command established, containment, cancellation, resolution, or clearing. Non-meaningful updates, such as duplicate acknowledgments, can be stored silently.

For the structure-fire example, the first dispatch creates an incident. The en-route message attaches quietly. The working-fire update attaches and updates the push. The additional-units dispatch attaches because of address and unit overlap. The containment and fire-out messages attach because of unit overlap and status progression. The result is one incident thread, not seven independent alerts.

### Speaker diarization
Diarization in v4 should focus on practical role labeling, not identity. Useful labels include dispatcher, unit, command, unknown, and overlapping/unreadable.

Possible approaches:

- **Text-first role inference.** Use phrases, unit labels, and context. This is cheap and likely good enough for an initial release.
- **Audio diarization model.** Evaluate pyannote-style or smaller local alternatives, but treat licensing, CPU cost, and packaging as open questions.
- **Hybrid.** Use text and unit context first; add optional audio speaker segmentation where available.

Speaker output should be confidence-bearing:

```json
{
  "speaker": {
    "role": "unit",
    "label": "Engine 41",
    "method": "text_plus_context",
    "confidence": 0.78
  }
}
```

If the speaker is uncertain, store `unknown`. Core alert delivery should not depend on audio diarization being enabled.

### Alert delivery
v4 delivery should send fewer interruptions while preserving all facts in the database and dashboard. The delivery layer decides whether an event creates an incident alert, updates an incident, sends a mesh summary, or stores silently.

ntfy behavior:

- use a stable incident identifier for repeated updates where supported;
- set a stable title such as `Structure fire — 1500 California Street`;
- include a concise body focused on what changed;
- use `X-Tags` for category and priority cues;
- use click action metadata to open the dashboard incident view;
- keep configured delivery destinations private and out of docs;
- test actual iOS behavior before promising exact in-place update semantics.

If a client cannot update in place, v4 should still reduce noise by suppressing non-meaningful updates and sending concise summaries.

Meshtastic behavior:

- keep messages compact;
- send initial, important update, and resolved messages;
- avoid pushing every timeline detail over mesh;
- prefer summaries such as `NEW Structure fire, 1500 California Street, smoke showing` or `CLR Structure fire: fire out, units returning`.

Delivery should be idempotent. Each attempt should be tied to incident id, event id or state version, destination, rendered content hash, timestamp, and result. This prevents duplicate notifications after restarts.

### Dashboard incident view
The v4 dashboard should make incidents the primary view while retaining raw event history.

Incident list should show:

- active incidents first;
- recently resolved incidents;
- cleared incident history;
- standalone events;
- title;
- state;
- current status;
- last update time;
- priority;
- confidence;
- units;
- location text if known.

Incident detail should show:

- current summary;
- state and status history;
- location card;
- optional local map pin;
- unit list;
- event timeline;
- raw transcript for every event;
- extracted fields;
- confidence labels;
- clustering evidence;
- notification and mesh delivery history.

The dashboard should visually distinguish raw transcript from model-derived summaries. Users should be able to answer: what happened, where it happened if known, which units were involved, what changed, why an event attached, and what evidence is uncertain.

---
## Migration from v3
### Strategy
The migration should be additive and non-destructive. Existing event rows remain readable. New incident tables and columns are added. The daemon can run in v4 mode, compatibility mode, or fallback mode depending on config and service health.

If migration fails, v4 mode should refuse to start and report a clear error. The migration should be repeatable and safe to run more than once.

### Database changes
New `incidents` table:

- `id`
- `created_at`
- `updated_at`
- `first_event_at`
- `last_event_at`
- `state`
- `incident_type`
- `title`
- `summary`
- `address_raw`
- `address_normalized`
- `address_kind`
- `address_lat`
- `address_lon`
- `address_confidence`
- `current_status`
- `priority`
- `cluster_confidence`
- `overall_confidence`
- `notification_thread_key`
- `last_push_at`
- `last_mesh_at`
- `metadata_json`

Changes to `events`:

- `incident_id` foreign key;
- `transcript_confidence`;
- `speaker_role`;
- `speaker_label`;
- `speaker_confidence`;
- `extracted_units_json`;
- `extracted_incident_type`;
- `extracted_address_json`;
- `extracted_status`;
- `extraction_confidence`;
- `priority`;
- `cluster_decision`;
- `cluster_confidence`;
- `model_metadata_json`.

Optional new tables:

- `incident_events` if candidate links or future manual merge/split tools need many-to-many relationships;
- `delivery_attempts` for idempotency, retry tracking, and dashboard diagnostics.

### Config changes
New `[transcription]` section:

- `mode = "streaming"`
- `whisper_server_url`
- `model_path`
- `language`
- `partial_results`
- `finalize_silence_ms`
- `max_segment_seconds`
- `batch_fallback`

New `[llm]` section:

- `enabled`
- `runtime`
- `server_url`
- `model_path`
- `model_name`
- `context_tokens`
- `temperature`
- `timeout_ms`
- `max_tokens`
- `structured_output`
- `fallback_on_error`

New `[incidents]` section:

- `enabled`
- `attach_threshold`
- `review_threshold`
- `active_window_minutes`
- `resolved_grace_minutes`
- `clear_after_minutes`
- `max_updates_per_incident`
- `meaningful_update_only`
- `auto_resolve`
- `known_units_file`
- `allow_same_address_multiple_incidents`

New `[diarization]` section:

- `enabled`
- `mode`
- `audio_model_path`
- `min_confidence`
- `store_speaker_embeddings`

New `[dashboard]` keys:

- `incident_view`
- `show_confidence`
- `show_legacy_events`
- `map_enabled`
- `external_geocoder`

Deprecated keys should warn and continue where possible. Warnings should explain the replacement setting. Alert templates that assume one event equals one notification need special migration notes.

### Legacy events view
The dashboard should keep a legacy events view for v3 data and low-confidence v4 debugging. Legacy rows may not have incident id, extracted fields, speaker labels, confidence, or notification thread metadata. The UI should display those fields as unavailable rather than attempting automatic backfill.

### Rollback plan
1. Stop the v4 daemon.
2. Disable incident and LLM behavior in config or restore a v3-compatible config.
3. Start compatibility mode.
4. Leave new tables and columns in place.
5. Continue writing v3-style event rows.

Rollback should not require dropping schema changes. If the old daemon cannot read the migrated database, v4 should ship a compatibility mode that behaves like v3 while using the new schema.

### Migration tests
Migration tests should cover empty databases, v3 databases with recent events, malformed rows, repeated migrations, restart during migration, mixed legacy/v4 dashboard behavior, fallback mode after migration, and delivery idempotency after restart.

---
## Resource budget
### Target hardware
The target profile is a Raspberry Pi 5 with 16 GB RAM. The design should leave headroom for the OS, Python daemon, dashboard, SQLite, `whisper.cpp`, local LLM runtime, audio buffers, and delivery workers. It should not assume a desktop GPU.

### CPU
Main CPU consumers:

- audio capture;
- VAD;
- streaming transcription;
- LLM extraction;
- semantic similarity scoring;
- optional diarization;
- dashboard requests;
- database writes.

Streaming transcription is the steady baseline. LLM extraction should be bursty because it runs on finalized events. Audio diarization may be expensive, so it should be optional.

### Memory
Main memory consumers:

- OS and services;
- daemon and dashboard;
- SQLite cache;
- transcription model;
- LLM model;
- audio buffers;
- prompt context and response buffers.

A quantized 3B-class model should be plausible on the target hardware, but exact headroom must be benchmarked. If memory pressure appears, reduce LLM context, use a smaller quantization, disable audio diarization, reduce partial retention, or fall back to simpler behavior.

### Storage
Storage growth comes from event rows, incident rows, extraction JSON, delivery logs, optional audio snippets, model files, logs, and dashboard assets. Model files dominate one-time storage. Database growth should remain modest if raw audio is not retained.

Recommended controls:

- metadata retention period;
- audio snippets disabled by default or short-lived;
- delivery log pruning;
- log rotation;
- periodic SQLite maintenance;
- dashboard display of approximate database size.

### Thermal expectations
Continuous transcription plus local LLM inference may require active cooling. Thermal throttling would affect transcription latency and extraction time. v4 should document cooling expectations, avoid requiring a specific case, and eventually expose temperature and throttling status if available.

### Guardrails
v4 should enforce maximum extraction time, maximum cluster scoring time, maximum active incidents considered per event, maximum prompt context size, dashboard query limits, delivery retry limits, and memory warning thresholds. If a guardrail is exceeded, the daemon should store the event and degrade gracefully.

---
## Open questions
1. **Which LLM should ship by default?** Choose based on extraction accuracy, clustering support, latency, memory, packaging, license compatibility, and noisy-transcript behavior.
2. **How should unit-number mishearing be handled?** Options include known unit lists, aliases, edit distance, context, confidence thresholds, and visible uncertainty.
3. **How should multiple incidents at the same address be disambiguated?** Use time, unit sets, incident type, status conflict, explicit new dispatch language, and prior cleared state.
4. **How long should incidents stay active?** Defaults may need to vary by incident type. Alarms may clear quickly; large fires or searches may last much longer.
5. **Should incident maps be shareable?** Default should be local-only. Any sharing should be opt-in and clearly labeled as unofficial.
6. **Should raw audio be retained?** Raw audio helps debugging but raises storage and privacy concerns. Default should likely be no long-term retention.
7. **How much manual correction belongs in v4.0?** Showing confidence and evidence is required; merge/split/edit tools may be later work.
8. **Should there be an evaluation dataset?** Public fixtures should be synthetic and generic. Private local fixtures should never be committed.
9. **How should overlapping or unreadable transmissions be represented?** Store low confidence, avoid forced clustering, and show uncertainty.
10. **What is the minimum viable diarization feature?** Text-derived role labels may be enough for v4.0; audio diarization may be experimental.
11. **How should model files be installed and updated?** Installer design needs download, checksum, replacement, rollback, and user-provided path support.
12. **How should clustering decisions be explained?** Store same-address, same-unit, time-window, status-progression, semantic, and conflict evidence.

---
## Risks & tradeoffs
### LLM hallucinations
The model may invent details. Mitigations: constrained prompts, low temperature, schema validation, confidence thresholds, raw transcript visibility, and deterministic fallback.

### False clustering
Unrelated events may be merged. This is the highest product risk because it can hide separate incidents behind one thread. Mitigations: conservative thresholds, conflict penalties, same-address disambiguation, stored evidence, and dashboard visibility.

### Missed clustering
Related events may stay separate. This reduces value but is usually safer than false merging. Mitigations: tune weights, use unit overlap, use recent context, allow late attachment, and measure recall.

### Latency regression
LLM extraction and clustering may slow alerts. Mitigations: streaming transcription, strict LLM timeouts, deterministic fast paths, deferred summaries, and stage-level latency metrics.

### Thermal load and fan noise
Continuous inference can heat the device. Mitigations: efficient model defaults, optional diarization, cooling guidance, and temperature monitoring.

### Complexity tax
v4 adds models, prompts, incident state, delivery threading, and migrations. Mitigations: isolate components, keep compatibility mode, add tests, and avoid overbuilding manual workflows.

### Notification-client differences
In-place updates may behave differently across ntfy clients and phone platforms. Mitigations: stable incident identity, conservative update frequency, documented tested behavior, and configurable formatting.

### Privacy leakage
Summaries, map links, dashboard URLs, and logs can expose sensitive local details. Mitigations: local-first defaults, no private examples in docs, no external geocoding by default, configurable retention, and secret-safe logging.

### Model packaging burden
Local models are large and can be confusing to install. Mitigations: clear default model, checksums, documented alternatives, user-provided model paths, and fallback without LLM.

### Evaluation difficulty
Scanner audio varies by receiver, feed, channel, agency cadence, and audio quality. Mitigations: synthetic public fixtures, private local evaluation, tunable thresholds, confidence display, and known-unit aliases.

---
## Milestones / phases
v4.0 can ship as one release, but the build should land in phases. Each milestone should leave the system runnable.

### M1: streaming Whisper integration
Scope:

- run `whisper.cpp` server mode locally;
- stream audio from the existing capture path;
- emit partial and final transcript events;
- store finalized transcripts through the existing event flow;
- measure transcription latency;
- keep batch fallback;
- add transcription service health checks.

Acceptance criteria:

- given active scanner audio, partial text appears within the target latency under benchmark conditions;
- given a final transcript, the existing keyword pipeline can process it;
- given streaming failure, the daemon logs the issue and recovers or falls back according to config;
- given batch fallback is enabled, v3-style behavior still works.

### M2: LLM extraction online
Scope:

- add `[llm]` config;
- run local LLM runtime;
- define extraction prompt;
- validate structured output;
- store units, type, address, status, and confidence;
- show extracted fields beside raw transcripts;
- record extraction latency and failures;
- keep keyword alerting as the primary delivery path.

Acceptance criteria:

- dispatch transcripts with type and location produce structured fields;
- low-information transcripts keep unknown fields unknown;
- invalid model output fails validation without dropping the event;
- dashboard shows raw transcript and extracted fields together.

### M3: incident clustering
Scope:

- add incidents schema;
- link events to incidents;
- implement initial cluster scoring;
- create incidents from high-confidence events;
- attach related events to active incidents;
- track lifecycle state;
- update summary and status;
- add incident list and detail views;
- add incident-aware ntfy and mesh delivery;
- store clustering evidence and confidence.

Acceptance criteria:

- the structure-fire example creates one incident with all seven events attached;
- meaningful escalation updates the existing notification thread or sends one concise update;
- minor acknowledgments are stored without unnecessary interruption;
- unrelated nearby events do not merge when evidence conflicts;
- closure language resolves the incident and sends a final update.

### M4: polish, migrations, docs, testing, ship
Scope:

- finalize migrations;
- add rollback and compatibility behavior;
- add config warnings;
- document model installation;
- document resource expectations;
- tune thresholds;
- add synthetic fixtures;
- test mixed legacy/v4 dashboards;
- test delivery rendering;
- test daemon restart behavior;
- write release notes.

Acceptance criteria:

- v3 databases migrate without losing legacy events;
- compatibility mode continues v3-style alerts after migration;
- missing LLM runtime does not break keyword fallback when enabled;
- dashboard separates active, resolved, cleared, standalone, and legacy records;
- clustering fixtures meet success targets or known failures are documented.

---
## Success criteria
### Product outcomes
- Average alert count drops by at least 60% for active multi-unit incidents in evaluation scenarios.
- At least 95% of dispatched calls in the evaluation set are correctly clustered with follow-up traffic.
- False clustering is rare enough that users trust incident threads.
- First useful notification is no slower than the v3 batch path under the target hardware profile.
- User feedback says notifications feel less noisy and easier to follow.

### Technical outcomes
- Streaming transcription produces partial text within the 1-2 second target under benchmark conditions.
- LLM extraction completes within configured timeout for typical transmissions.
- Clustering decisions store evidence and confidence.
- Raw transcripts remain available for every stored event.
- Daemon restart does not duplicate recent incident notifications.
- Fallback mode preserves v3-style keyword alerting when LLM or streaming services are unavailable.
- Migrations are repeatable and non-destructive.

### Metrics
- transcript latency p50/p95;
- extraction latency p50/p95;
- cluster attach precision;
- cluster attach recall;
- false-new-incident rate;
- false-merge rate;
- notification count per incident;
- meaningful-update delivery rate;
- LLM timeout rate;
- extraction validation failure rate;
- CPU utilization;
- memory utilization;
- device temperature;
- database growth per day.

---
## Appendix A: structure-fire walkthrough
### Input transmissions
| Time | Speaker | Transmission |
| --- | --- | --- |
| 13:42:18 | Dispatch | "Engine 41, structure fire, 1500 California Street, smoke showing" |
| 13:42:35 | Engine 41 | "Engine 41 en route" |
| 13:48:02 | Engine 41 | "Engine 41 on scene, working fire, requesting additional units" |
| 13:49:14 | Dispatch | "Engine 48, Truck 12, respond to 1500 California with Engine 41" |
| 13:55:30 | Engine 48 | "Engine 48 on scene" |
| 14:03:45 | Engine 41 | "Engine 41 has command, fire is contained to one room" |
| 14:18:22 | Engine 41 | "Engine 41 fire out, units returning" |

### Extraction examples
```json
{
  "units": ["Engine 41"],
  "incident_type": "structure_fire",
  "address": "1500 California Street",
  "status": "dispatched",
  "confidence": 0.82
}
```

```json
{
  "units": ["Engine 41"],
  "incident_type": "structure_fire",
  "address": null,
  "status": "working",
  "confidence": 0.84
}
```

```json
{
  "units": ["Engine 41"],
  "incident_type": "structure_fire",
  "address": null,
  "status": "fire_out",
  "confidence": 0.86
}
```

### Incident timeline
```text
13:42 new      Structure fire at 1500 California Street; smoke showing.
13:42 active   Engine 41 en route.
13:48 updating Engine 41 on scene; working fire; more units requested.
13:49 updating Engine 48 and Truck 12 assigned.
13:55 active   Engine 48 on scene.
14:03 updating Command established; fire contained to one room.
14:18 resolved Fire out; units returning.
```

### Result
v3 sends one alert per matching transmission. v4 creates one incident, stores all seven events in the timeline, updates the incident for meaningful developments, and resolves it on the final closure message.

---
## Appendix B: public documentation privacy checklist
Before committing public docs, verify they do not include:

- private IP addresses;
- private hostnames;
- private notification destinations;
- private coordinates;
- private city, county, or state references;
- private unit-watch keywords;
- private pattern expressions;
- names of specific people;
- local dashboard URLs;
- secrets, tokens, or API keys;
- screenshots containing private notification content.

Examples should use generic addresses, generic units, synthetic timestamps, and placeholders only.

---
## Appendix C: design principles
1. **The transcript is evidence; the incident is interpretation.** Preserve both and keep them visually distinct.
2. **Prefer conservative automation.** Two incident cards are better than incorrectly merging unrelated incidents.
3. **Make uncertainty visible.** Confidence should appear in the dashboard, not only in logs.
4. **Keep local-first defaults.** Core intelligence should run offline except for configured delivery endpoints.
5. **Preserve fallback paths.** Keyword relay remains the safety net.
6. **Optimize for usefulness, not completeness.** v4 does not need every dispatch nuance; it needs clearer, quieter, evidence-backed alerts.

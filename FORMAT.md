# `facebook-archive-normalized/1`

This repository owns this small interchange contract. It is not a Meta schema.
JSON strings are Unicode. All timestamps are integral Unix seconds, except
message `timestamp_ms` in milliseconds; emitted millisecond values reflect only
the actual source precision. Unknown fields are not synthesized.

Top-level keys:

| Key | Meaning |
| --- | --- |
| `format` | Exactly `facebook-archive-normalized/1` |
| `text_encoding` | Exactly `Unicode` |
| `profile` | `name`, `username`: observed profile identity strings |
| `posts` | Ordered post occurrences described below |
| `threads` | Separate observed or explicitly coalesced contexts |
| `posted_media` | Supported posted media occurrences |
| `provenance` | Per-source parsing evidence; union evidence lives in sibling ledger |

Post: `location`, `timestamp`, `action_title`, `text` (ordered string array),
`attachments` (ordered array). Text is authored prose; action title stays distinct.
Attachments are either `{kind: "media", uri, creation_timestamp}` or
`{kind: "link", url, label}`. Local `uri` is relative to the bundle `archive/`,
with safe POSIX segments. Media time means exported posting time, not camera time.

Thread: `location` (derived context/compatibility JSON path), `observed_senders`
(string array), `messages` (ordered occurrences). Message: `location`, `sender`,
`timestamp_ms`, `text`, `attachments`. A message attachment is
`{kind: "image"|"video", uri, creation_timestamp}`. Shared URLs not already in
message prose are appended after a blank line; original DOM/source hash remains
in provenance. No silent recipient or full membership claim is made.

Posted medium: `location`, `uri`, `creation_timestamp`, `title`, `description`.
Album source location is provenance only. No EXIF/camera metadata is invented.

Locations identify deterministic derived occurrences using a compatibility member
path and JSON pointer, or a derived thread path. They are **not** Facebook IDs,
are not independent evidence of identity, and may change with a recipe change or
changed input ordering. No invented platform IDs are emitted. Provenance includes
source-relative path, HTML hash, selector/index, original timestamp and its basis,
normalized epoch and mapped fields. A union ledger retains each generation's
observation separately from the chosen compatible representation.

Source duplicate occurrences remain separate. Array order is deterministic for
a fixed recipe and input. Coalesced union order is deterministic by content/context
key and occurrence. No timestamp jitter or implicit newer-wins replacement.
Compatibility encoding and path relocation are adapters, not this text contract.

The normalized document is lossless only for the converter's supported semantic
subset. Unsupported fields/categories remain in original HTML, with explicit
coverage/refusals. A format/recipe change requires reconversion and native proof;
an import job's success alone proves neither complete coverage nor fidelity.

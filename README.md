# Facebook archive converter

Offline Python 3.11+ standard-library converter for **observed English legacy
Facebook HTML layouts**. Produces a versioned Unicode interchange document, with
optional modern Meta-shaped JSON or an explicit Timelinize compatibility adapter.
No network access, source writes, database operations, or application deployment.
MIT licensed. Initial implementation and tests developed with AI assistance.

This is **not an official Meta export schema**, a complete Facebook exporter, or
a generic HTML scraper. No formal Meta schema/version is claimed. Export year
is provenance, not schema version. Keep original archives unchanged.

## Run

No installation or dependencies required:

```sh
python3 -B convert.py /path/to/export --source-timezone America/New_York
python3 -B convert.py /path/to/export --source-timezone America/New_York \
  --media copy --output /private/new-bundle
```

Source must be an extracted export root containing `index.html`; no extraction is
performed. Without `--output`, prints aggregate coverage only. `--evidence NEW`
writes private reports/provenance even when records are refused. Output must be
new, outside the source, with an existing parent. Directories use 0700; generated
files use 0600. Output contains private names, paths and text; do not publish it.

Optional installation: `python3 -m pip install .` provides
`facebook-archive-convert` and `facebook-archive-coalesce`.

By default any record refusal blocks output. Add `--allow-partial` only for an
explicit supported subset; every refusal remains in `refused-records.json`.
Exit 2 means unresolved records, including a successfully written partial bundle.
Exit 0 means no refusals in supported categories, **not complete archive coverage**.
Unsupported categories are counted in `report.json`.

`--source-timezone` supplies an IANA timezone only when established for this export.
A declaration in `location/timezone.html` takes precedence; conflicting operator
input is refused. No current-local-timezone default. Explicit epoch/offset dates
are honored. Local DST folds/gaps, invalid dates, missing timezones remain refused.
`--source-utc-offset=-04:00` is available only for an established fixed offset
applicable to the whole input; do not use it to guess individual ambiguous times.
Minute-resolution source dates remain identified in provenance. Epoch seconds do
not invent second-level precision.

## Outputs and targets

Every written bundle contains:

- `normalized.json`: [Unicode interchange contract](FORMAT.md), independent of any
  consumer. Locations identify derived occurrences, never claimed platform IDs.
- `archive/`: selected output and referenced media only.
- `archive.zip`: portable archive, only with `--media copy`; sorted stored members,
  fixed ZIP timestamps and permissions. Media copying/hashing streams in chunks.
- `report.json`, `provenance.json`, `refused-records.json`, `source-manifest.json`:
  coverage, source HTML SHA-256, source stat inventory, source record locations,
  original dates, timestamp basis, field mappings and media mappings/hashes.
- `INCOMPLETE`: remains after any write/final-check failure. Never consume a bundle
  retaining this marker.

Default `--media reference` uses local symlinks and makes no ZIP. It avoids copying
media during review and is not portable. `--media copy` verifies each copied file
against source SHA-256. Pre/post publication checks compare the full source stat
inventory and parsed HTML hashes. This is not a fresh preservation audit of every
unreferenced media byte. Source symlinks, nonregular files, unsafe/missing media,
pre-existing JSON and source/output overlap are refused. Remote media is never
fetched; counts identify unfetched references.

Targets, selected explicitly with `--target`:

| Target | Archive payload | Text policy |
| --- | --- | --- |
| `normalized` (default) | `normalized.json` plus `media/` | Unicode |
| `meta-json` | Observed modern Meta-shaped subset paths | Unicode; not an official schema |
| `timelinize` | Same subset and derived media prefix required by native byte walker | UTF-8 bytes represented as Latin-1 code points on specific repaired fields |

Compatibility targets also retain Unicode `normalized.json` beside `archive/`.
Its media URIs resolve relative to `archive/`. The Timelinize adapter transforms
profile full name, participant/sender names, message content, authored post text,
and posted-media title/description. Action titles, URLs, filenames and link labels
remain Unicode. JSON escaping alone is distinct from this explicit byte encoding.
Never feed `meta-json` Unicode text into an importer that unconditionally repairs
Latin-1 mojibake. No deployment dependencies exist in the parser or interchange.

Timelinize output requires an occurrence-safe modern importer, such as the
[bioshazard fork](https://github.com/bioshazard/timelinize), plus native identity
constraints `timestamp`, `data`, `data_source_name`, `classification_name`, and
`intermediate_location` all enabled. It preserves same-time exact text occurrences
and case variants. An older importer may collapse them. No native import fidelity
is claimed by these source-only tests; prove the selected app revision separately.
Repeated references to one media URI may still coalesce in the native consumer;
context may retain its last observation. This converter does not fabricate media
IDs or change dates to work around consumer limitations.

## Observed layout coverage

Supported English DOM profiles use `_2lej` record boxes, `_2lel` headers,
`_2let` bodies and `_2lem` date footers. Profile identity uses `Name`/`Username`
table rows. Posts accept `posts/your_posts.html` or numbered chunks; having both
forms is ambiguous and refused. Messages accept `messages/THREAD/message.html`
and numbered modern directory/chunk forms, with the observed four-slot body
(attachments, text, shares, reactions) plus verified extra attachment/IP/reaction
slots. Empty or missing senders are refused; no guessed actors.

Coverage: profile name/username, authored post text, supported shared links/media,
message text/attachments, and posted photos/videos including observed album pages.
Image/video formats: JPEG, PNG, GIF, MP4, MOV. Structured post prose is not
reconstructed as authored text. Actual `Updated DATE` labels are distinguished
from prose beginning “Updated”. Metadata-only/empty message boxes are counted.

Explicit limits: comments, reactions, friends, events, locations and remaining
categories are unconverted. Audio/documents/unknown layouts are refused. Album
membership and source upload metadata remain in provenance/source, not inferred
native collections or captions. Known message IP annotations remain source-only.
Message participants are **observed senders only**: silent participants/history
cannot be recovered. Source names do not establish distinct human identities;
a native importer may conflate same-name people. Consult counts and omissions.

## Coalesce two reviewed generations

Convert both original roots with the current recipe first. Generation labels are
operator provenance labels, **not schema versions**. The bundles' original roots
remain required for source verification and media byte hashes.

```sh
python3 -B coalesce.py --earlier /private/earlier-bundle \
  --later /private/later-bundle --earlier-source /path/to/earlier-export \
  --later-source /path/to/later-export --earlier-label earlier \
  --later-label later --target timelinize --output /private/new-union
```

Only two generations are supported; this is not an ingestion framework. There is
no assumption that later supersedes earlier. The algorithm compares all supported
record fields and media bytes. Exact repeated observations use the **maximum**
generation multiplicity. Distinct coincident variants remain separate. Unique
compatible missing title/caption metadata may be filled from the earlier copy;
conflicting nonblank values remain separate. Empty post date/title alone cannot
establish correspondence. Empty posts without primary content are quarantined;
same-time text collision groups are retained for occurrence-safe consumers.

Thread correspondence requires three distinct globally unique exact message
anchors of at least 24 UTF-8 bytes, equal observed sender sets, and reciprocal
uniqueness. Names alone never match threads. Short or ambiguous histories remain
separate, which may retain duplicate cross-generation observations rather than
risk merging unrelated conversations. These are content correspondences, not
recovered platform IDs.

Union outputs add `union-ledger.json` (every selected/quarantined occurrence and
all source observations), `thread-correspondence.json`, per-generation coverage/
refusal/source manifests, `media-manifest.json` and `package-sha256.json`.
Union archives always copy and rehash referenced media, and are explicitly partial.
`--verified-media-cache PRIOR_UNION` optionally reuses previous hashes/copies only
with a complete archive matching its package hash; source stat/HTML checks and
new copied-byte verification still run. It does not reuse timestamps or identities.
The source stat/cache assumption is explicit; omit this option for fresh hashes.

## Verify and develop

```sh
python3 -B -m unittest discover -v
```

Tests generate synthetic DOM/media only. They exercise both observed layout forms,
Unicode and explicit byte adapters, dates/DST/timezone refusals, source/output
safety, media hashes, normalized round trips, portable archives, CLI output,
thread anchors, conflicting metadata, and maximum occurrence multiplicity.
There are no private fixtures, deployment tools, database patchers or credentials.

Parser/coalescer ancestry: user-owned commissioned implementation originally in
`biosnet` commit `f02280f`, `tools/facebook-export-conversion/{convert,coalesce}.py`.
Moved and generalized here with the owner's authorization; MIT applies here.
Timelinize implementation/licensing stays in its own repository. Legacy HTML
parsing belongs here and must not be copied into Timelinize.

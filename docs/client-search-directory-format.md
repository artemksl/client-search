# Supplied clinic directory format

Prepare one UTF-8 CSV file for one city and country. Put one physical clinic
branch on each row and give it an explicit buying/contact account. The user
obtains the clinic information elsewhere and is responsible for its contents.

The [Client research panel](client-research-panel.md) previews and imports this
file, assesses clinics, and supports separately enabled recurring checks. The
standalone parser and local validator only check the supplied format. The
existing immutable research-release importer keeps its current format.

Start with [the blank template](examples/client-search-directory-template.csv)
or [the fictional London example](examples/client-search-directory-example.csv).
The template contains only the header; add at least one branch row before
validation.

## Ask an AI chatbot to research the directory

In **Import clinic directory**, choose **Research prompt** beside **Download CSV template**.
Enter the treatments as `treatment-id = treatment name`, one per line; optionally
list the areas to cover. **Copy research prompt** fills in the selected city's
country, language and exact scope, then copies the complete prompt. **Create
research prompt** lets you preview and download it instead. Give it to a chatbot
with web research. Attach the previous CSV when updating an existing list so its
stable IDs can be reused.

The [research prompt template](../src/clinic_geo/web/static/client_research_directory_prompt.txt)
contains a versioned plain-text data contract, without JSON Schema. Each numbered
column specifies its type, required/empty semantics, exact value rules,
relationships across rows, and valid/invalid examples. Shared type definitions
cover Unicode text, case-sensitive IDs, country codes, URLs and pipe-separated
lists. The prompt also defines source verification, branch deduplication and
buying-account grouping. It requests two separate outputs:

- `clinics.csv`: the exact 12-column import file, with only supported fields.
- `research-notes.md`: source URLs, dates, account-grouping evidence, coverage
  gaps, incomplete candidates and changes to an earlier directory, linked by ID.

The canonical AI export uses UTF-8 without a BOM, an exact unquoted header,
fully quoted data cells, one physical line per branch, LF line endings, sorted
records and lists, and `""` for an empty alias cell. It includes a clearly
fictional two-branch CSV showing quotes, commas, Unicode and empty values, and
asks a file-capable chatbot to write and reopen the CSV for validation. The
specification is included in the copied/downloaded research prompt; do not
insert it as rows in the import CSV.

These generation rules deliberately choose one strict export style. The
existing importer remains compatible with its accepted CSV variations described
below, including optional alias columns, BOMs and ordinary CSV quoting. The
text contract instructs the chatbot; only applicable importer checks are
automatically enforced. The importer does not verify research evidence.

Review both outputs before importing. Required facts must be verified; missing
facts belong in the notes rather than guessed CSV cells. Unconfirmed account
relationships remain separate provisional accounts for your review. A source
file is not proof of complete city coverage or a shared buyer. Importing saves
the directory; AI identification and recommendation checks start separately.

## Validate a file

From the repository root:

```sh
.venv/bin/python scripts/validate_client_search_directory.py /path/to/clinics.csv
```

The command prints a JSON summary or validation errors. It writes nothing and
performs no browser, model or network calls. A valid file confirms that the
directory satisfies the format; it does not verify the clinics, websites,
ownership, treatment offerings or commercial evidence.

## Columns

Use these exact, case-sensitive column names. Every required cell must be
nonempty. The optional alias columns may be omitted or left blank.

| Column | Required | Meaning |
|---|---|---|
| `account_id` | Yes | Stable ID for one buying/contact decision unit. Repeat it for branches with the same buyer. |
| `account_name` | Yes | Display name of that account. Keep it identical on every row with its account ID. |
| `branch_id` | Yes | Stable ID for this physical branch, unique across the file. |
| `branch_name` | Yes | Display name identifying this branch. |
| `country_code` | Yes | Two uppercase ASCII letters, such as `GB` or `DE`, representing the country. |
| `city` | Yes | The one city covered by this file. Keep it identical on all rows. |
| `area` | Yes | Explicit local area assigned to the branch. Use consistent labels for areas you intend to measure together. |
| `address` | Yes | The branch's full physical address. International text and address formats are accepted. |
| `website` | Yes | An absolute `http://` or `https://` URL with a host and no embedded credentials. |
| `treatment_ids` | Yes | One or more treatment IDs separated by `|`, identifying offerings at this branch. |
| `account_aliases` | No | Other literal names for the buying account, separated by `|`. Keep the list identical for every row with its account ID. |
| `branch_aliases` | No | Other literal names specifically identifying this branch, separated by `|`. |

All rows must have the same country code and city. Local areas, branch names
and addresses may differ. Keep another city in a separate file, even when an
account also operates there.

IDs are supplied by the user. An ID must be 1–128 characters, start with an
ASCII letter or digit, and contain only ASCII letters, digits, `_`, `.`, `:`
or `-`. The pattern is `[A-Za-z0-9][A-Za-z0-9_.:-]*`. IDs are case-sensitive:
`clinic-01` and `Clinic-01` are different IDs. Keep them stable when preparing
later versions of the directory. The parser does not generate IDs from row
numbers, names, addresses or website domains.

## Accounts and branches

An account represents the unit you would contact for a buying decision. A
branch represents a physical clinic location. A single-location business still
needs both IDs.

For a centrally managed organisation, repeat the same `account_id`, account
name and account aliases across its branches. Give each branch its own
`branch_id`, address, area and treatment list.

For independently operated franchises or separate buyers, use different
account IDs. A shared brand name, website or domain does not establish a shared
buying decision. Names and addresses can also be shared by separate businesses.
The parser does not merge any of these automatically or infer country, area,
branch identity or account ownership.

Account aliases and branch aliases have different meanings. An account alias
must not be treated as evidence that every branch was mentioned. A name that
could refer to several branches requires later assessment to preserve that
uncertainty; parsing the file does not resolve it.

## CSV and list rules

- Save as UTF-8. A leading UTF-8 byte-order mark is accepted.
- Use commas between columns and normal CSV quoting. Quote a value containing
  a comma, quote or line break; double a quote inside a quoted value.
- Use the exact supported header names. Do not add score, visibility or other
  custom columns to this format.
- Separate aliases and treatment IDs with `|`. This character is reserved as
  the list separator and cannot be part of an individual entry.
- A treatment list must contain at least one entry. Empty or duplicate entries
  are invalid; `botox||laser` and `botox|botox` are invalid.
- A blank alias cell means no aliases. If a list is supplied, use nonempty,
  distinct entries rather than leading/trailing separators or repeated names.
- Repeated account IDs must have consistent account names and alias lists.
- A repeated branch ID is accepted only for an exact duplicate row. Exact
  duplicate rows collapse into one branch; conflicting duplicates are errors.
- Different IDs remain different records. Correct IDs in the source file when
  two rows represent the same account or branch.

Leading/trailing cell whitespace is removed and list order is ignored when
comparing rows. Apart from those formatting differences, duplicate rows must
agree on every field. Treatment IDs use the same stable ID syntax described
above; validation does not check them against a treatment catalogue.

The website is checked as a URL. It is not opened, and its existence or
ownership is not established by validation.

## London: an organisation and independent franchises

The [worked CSV](examples/client-search-directory-example.csv) contains four
fictional branches:

| Account ID | Branch ID | Buying relationship |
|---|---|---|
| `harbour-skin` | `harbour-marylebone` | Same central buyer as the Soho branch. |
| `harbour-skin` | `harbour-soho` | Same central buyer as the Marylebone branch. |
| `luma-franchise-north` | `luma-camden` | Independent franchise buyer. |
| `luma-franchise-south` | `luma-brixton` | Separate franchise buyer despite the shared brand/domain. |

There are three accounts and four branches. The independent Luma franchises
deliberately share the website `https://luma-skin.example`; their account IDs
keep their buying decisions separate.

## München: a separate international file

Non-ASCII clinic names, city names, areas and addresses are allowed. This
fictional example belongs in a separate file from the London example:

```csv
account_id,account_name,branch_id,branch_name,country_code,city,area,address,website,treatment_ids,account_aliases,branch_aliases
isar-aesthetik,Isar Ästhetik,isar-lehel,Isar Ästhetik Lehel,DE,München,Lehel,"Isarstraße 12, 80538 München",https://isar-aesthetik.example,botox|laser,Isar Aesthetic,Isar Ästhetik – Lehel
```

Use an uppercase country code and explicit area even when the name/address
contains that information. The parser does not depend on UK postcodes or an
English name to establish those fields.

## Public Python API

Consumers import through the outreach facade:

```python
from clinic_geo.outreach.api import (
    load_supplied_clinic_directory,
    parse_supplied_clinic_directory,
)

directory = load_supplied_clinic_directory("/path/to/clinics.csv")
# For CSV text that has already been read:
directory = parse_supplied_clinic_directory(csv_text)
```

Both functions return a `SuppliedClinicDirectory` after validating the entire
file. They do not persist records, launch checks or feed the existing prospect
rubric automatically. A valid import contract is the first step toward those
application workflows; use the panel to preview and save a directory.

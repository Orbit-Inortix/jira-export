# jira-export

Export a Jira Cloud project to JSON files with every field, comment, work log and history entry, and download its attachments. The files are made for the Jira import of [Orbit by Inortix](https://orbit.inortix.com/), but they are plain Jira REST API v3 data, so you can also keep them as a full backup of a project.

Two small Python scripts, standard library only, no install step:

| Script | What it does |
| --- | --- |
| `jira_export.py` | Reads a Jira Cloud project (or any JQL) and writes `jira-<KEY>.json` files in the `orbit-jira-export/1` format. With `--attachments DIR` it also downloads the attachment files. |
| `jira_attachments_upload.py` | After the Jira import has run in Orbit, sends the downloaded attachment files to the matching Orbit work items. |

> **Status.** Orbit is in private preview and is not sold yet. Its Jira Cloud import works end to end and is in final testing. See [Import from Jira](https://orbit.inortix.com/docs/orbit/import-from-jira/) for what is matched and what is not carried.

## Requirements

- Python 3.10 or later.
- A Jira Cloud site and an Atlassian account that can browse the project. The export contains what that account can see.
- An Atlassian API token, created at <https://id.atlassian.com/manage-profile/security/api-tokens>.

## Export a project

```sh
export JIRA_API_TOKEN='<your Atlassian API token>'
python3 jira_export.py --site https://acme.atlassian.net --email you@acme.com --project ACME
```

Without `JIRA_API_TOKEN` the script asks for the token and does not echo it.

| Option | Meaning |
| --- | --- |
| `--site` | Your Jira Cloud address. Must be `https://`. |
| `--email` | The Atlassian account the token belongs to. |
| `--project` | A project key. Exports the whole project in key order. |
| `--jql` | A JQL query instead of a whole project, for example `project = ACME AND created >= -365d`. |
| `--out` | File name prefix. Default `jira-<project>`. |
| `--per-file` | Work items per file, 1 to 2,000. Default 1,000. |
| `--attachments DIR` | Also download every attachment to `DIR/<attachment id>/<file name>`. |

What you get:

- One or more files, `jira-ACME.json` or `jira-ACME-1.json`, `jira-ACME-2.json` and so on. Each file holds at most 2,000 work items and stays under 9 MiB, below the 10 MB that Orbit takes per file.
- Parents come first: epics, then standard work, then subtasks. Upload the files in the order they were written, so every parent exists before its children.
- Every field, with the names of custom fields, the full changelog, and every comment and work log. Jira cuts these short in search results; the script reads the rest page by page.
- A work item that alone is larger than 9 MiB is skipped and named, because its comments or history are too long to import.

Running it again writes the files again. With `--attachments`, files already downloaded are skipped, so an interrupted download can be resumed.

## Import the files in Orbit

In your Orbit project, open the Jira import (project settings, Import) and upload each file in order. Only project administrators can start a Jira import. The wizard maps the fields, shows a compatibility report and runs the import in the background. See [Import from Jira](https://orbit.inortix.com/docs/orbit/import-from-jira/).

## Send the attachments

Attachments follow after the import has run. Orbit lists each work item's Jira attachments in its origin, and this script sends the downloaded files to them.

```sh
export ORBIT_API_TOKEN='<your Orbit API token>'
python3 jira_attachments_upload.py --api https://acme.orbit.inortix.com --import <import id> --dir jira-ACME-files
```

- The token is a personal API token with write scope, of an administrator of the project you imported into. See [API authentication](https://orbit.inortix.com/docs/integrations/api-authentication/).
- `--import` is the id of the Jira import in Orbit.
- Each file goes through Orbit's usual upload rules: a safe file name, a content check, no executables, and at most 100 attachments per work item. Files over 10 MiB are skipped.
- A file Orbit already has is reported and not copied twice, so you can run the script again after a stop.
- A refused file is reported and the others go on. An answer about the token or the import stops the run. The exit code is 1 when any file was refused or skipped.

## Security

- The Jira token is read from `JIRA_API_TOKEN` or asked for. It is sent only to the Jira site you name, and redirects are never followed, so it cannot be carried to another host. The Orbit token is handled the same way and sent only to the Orbit address you name.
- Both addresses must be `https://` (the upload script also accepts `http://localhost` for testing).
- Downloaded file names are made safe for every operating system: no paths, no control characters, no reserved Windows names, and nothing outside `DIR`.
- Busy answers (429, 502, 503, 504) are retried with backoff, honouring `Retry-After`.
- The exports and attachments contain your Jira data. The `.gitignore` here keeps them out of Git; store them as you would store the Jira project itself.

## Tests

```sh
python3 -m unittest -v test_jira_export
```

The tests replace the network with a stand-in Jira and a local stand-in for Orbit, so they need neither.

## License

Apache License 2.0. See [LICENSE](LICENSE).

Jira and Atlassian are trademarks of Atlassian. This project is not affiliated with or endorsed by Atlassian.

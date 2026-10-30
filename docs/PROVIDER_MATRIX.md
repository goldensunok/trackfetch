# Provider matrix

What is implemented, what is covered by automated tests, and what still needs
verifying on a real machine with the real service running.

The distinction that matters:

- **Mock / API-fixture tested** means the provider was exercised against its own
  documented API with the HTTP (or SSH) boundary replaced by fixtures that carry
  the shapes the real service returns. This proves Trackfetch calls the right
  endpoints, sends the right bodies and understands the replies.
- **Live integration verified** means it was run against the actual program or
  network. **Nothing in this table is live-verified.** The build environment has
  no Soulseek access, no torrent client, no Usenet account and no SFTP or WebDAV
  server, and claiming otherwise would be false.

| Provider | Implemented | Automated tests passing | Mock / API-fixture tested | Live integration verified |
| --- | --- | --- | --- | --- |
| Soulseek (slskd) | Yes | Yes (97 tests) | Yes | **No. Needs verifying on your computer.** |
| Web download (HTTP/HTTPS) | Yes | Yes (23 tests) | Yes | No |
| qBittorrent | Yes | Yes (12 tests) | Yes | No |
| Transmission | Yes | Yes (6 tests) | Yes | No |
| Deluge | Yes | Yes (4 tests) | Yes | No |
| JDownloader | Yes | Yes (8 tests) | Yes, including the real signing and encryption | No |
| aria2 | Yes | Yes (4 tests) | Yes | No |
| SFTP | Yes | Yes (8 tests) | Yes, against a Paramiko stand-in | No |
| WebDAV | Yes | Yes (8 tests) | Yes | No |
| Syncthing | Yes | Yes (6 tests) | Yes | No |
| NZBGet | Yes | Yes (7 tests) | Yes | No |
| SABnzbd | Yes | Yes (5 tests) | Yes | No |

Total: **325 automated tests, all passing.** Counts per provider include
its client tests, its provider tests, the end-to-end journey and the
regression tests in `tests/test_regressions.py`.

## What the Soulseek tests cover

The specification singled this out, so here is the list explicitly.

| Requirement | Where |
| --- | --- |
| slskd connection | `test_slskd_client.py::test_api_root_is_the_documented_v0_path`, `test_slskd_provider.py::test_connected_status_is_reported` |
| Authentication | `test_login_posts_credentials_and_stores_bearer_token`, `test_api_key_is_sent_as_the_documented_header` |
| Unavailable slskd | `test_slskd_not_running_is_reported_as_a_connection_problem`, `test_slskd_not_running_gives_a_plain_english_status` |
| Soulseek disconnected | `test_soulseek_disconnected_is_detected`, `test_soulseek_signed_out_is_explained_not_dumped` |
| Search creation | `test_search_start_posts_the_documented_body` |
| Asynchronous result collection | `test_run_search_waits_for_completion_then_collects_responses` |
| Multiple users and results | `test_search_returns_candidates_from_several_users` |
| Metadata parsing | `test_search_exposes_every_available_metadata_field`, `test_parse_search_files_exposes_all_available_metadata` |
| Manual result selection | `test_only_the_selected_file_is_enqueued`, `test_end_to_end_soulseek.py::test_the_whole_soulseek_journey` |
| Enqueueing an explicitly selected file | `test_only_the_selected_file_is_enqueued` |
| Transfer progress | `test_poll_reports_progress`, `test_poll_reports_remote_queue_position` |
| Completion | `test_poll_reports_completion` |
| Failure | `test_poll_reports_failure_in_plain_english`, `test_enqueue_failure_becomes_a_readable_job_error` |
| Timeout | `test_timeout_is_translated`, `test_slskd_supervisor.py::test_waiting_gives_up_rather_than_hanging` |
| Malformed API responses | `test_malformed_json_is_reported_readably`, `test_parse_search_files_tolerates_rubbish`, `test_flatten_transfers_tolerates_rubbish`, `test_search_survives_a_malformed_responses_payload` |
| Older and newer slskd builds | `test_enqueue_falls_back_to_the_older_endpoint`, `test_server_state_falls_back_to_application_state_on_older_builds` |

## Core tests

| Requirement | Where |
| --- | --- |
| Pasted-list parsing | `test_input_parser.py` |
| TXT input | `test_txt_import`, `test_txt_import_handles_a_byte_order_mark_and_windows_line_endings` |
| CSV input | `test_csv_import_picks_the_obvious_column`, `test_csv_with_semicolons_is_handled` |
| URL input | `test_links_magnets_and_files_are_recognised` |
| Persistent state | `test_storage.py::test_a_batch_survives_a_restart` |
| Resume | `test_transfer_manager.py::test_the_queue_is_restored_after_a_restart`, `test_end_to_end_soulseek.py::test_the_list_and_the_queue_survive_a_restart` |
| Duplicate handling | `test_destinations.py`, `test_transfer_manager.py::test_an_existing_file_is_renamed_rather_than_overwritten` |
| Safe destination paths | `test_destinations.py::test_filenames_are_made_safe`, `test_safe_join_refuses_to_escape` |
| Concurrent database operations | `test_storage.py::test_many_threads_can_write_at_once` and two others |
| No encoded preferences | `test_filters_and_relevance.py::test_live_remix_and_bootleg_are_not_penalised` |
| Secrets never logged | `test_security_and_diagnostics.py` |

## Regression tests

`tests/test_regressions.py` guards eight defects found during an adversarial
review of the finished code and fixed before handover:

| Defect | Guarded by |
| --- | --- |
| A Soulseek job stranded in Queued when slskd forgot the transfer | `test_a_transfer_slskd_has_forgotten_eventually_fails_rather_than_hanging` |
| A stale slskd record overwriting a retried job | `test_a_stale_record_does_not_overwrite_a_retried_job` |
| Two jobs for one file sharing a single transfer record | `test_two_jobs_for_the_same_file_do_not_share_one_record` |
| A removed transfer reappearing after the worker finished | `test_a_removed_transfer_does_not_come_back` |
| A failed resume causing the same job to be submitted twice | `test_a_failed_resume_does_not_submit_the_job_twice` |
| A resumed web download double-counting its own size | `test_a_resumed_download_does_not_double_count_its_size` |
| A stale cancel flag killing the first retry | `test_cancelling_a_waiting_download_does_not_break_the_next_attempt` |
| A file with no extension being saved as a folder | `test_a_file_with_no_extension_is_not_turned_into_a_folder` |

Two further problems found in the same review were fixed without a dedicated
test: blocking network calls on the interface thread (every queue button and URL
lookup now runs on a worker), and the thread pool being sized once at start
while the setting was read live.

## What still needs verifying on your computer

These are the things that genuinely cannot be proven anywhere but on the machine
that will run Trackfetch:

1. **Soulseek end to end.** That slskd installs, starts, signs in to the real
   Soulseek network, and that a real search returns real results which really
   download. This is the important one.
2. **The single-login rule.** That closing the ordinary Soulseek program is
   enough for Trackfetch to sign in on the same account, and that the warning
   appears when it is open. The detection code is tested, the behaviour of the
   Soulseek servers is not something a test can prove.
3. **The Windows credential store.** That `keyring` uses Windows Credential
   Manager rather than falling back to the local file. The diagnostic report
   states which one is in use.
4. **The installer.** That `TrackfetchSetup.exe` installs, the shortcuts work,
   and Windows SmartScreen can be got past. The build is automated and the
   frozen program was confirmed to start, but a signed installer was not
   produced, so SmartScreen will warn on first run.
5. **Each secondary provider against the real program**, if and when you set one
   up. Every one of them has been written against its published API and tested
   against fixtures of that API, but versions drift.

## Known limitations, stated plainly

- **Syncthing cannot serve file contents over its REST API.** There is no such
  endpoint. Trackfetch therefore browses through the API and copies the file out
  of the folder Syncthing already keeps on this computer. If the file has not
  finished synchronising, Trackfetch says so rather than pretending.
- **Soulseek has no pause.** slskd can queue, cancel and retry a download, but
  not pause one mid-flight. The Pause button is disabled for Soulseek rows
  rather than failing when pressed.
- **qBittorrent renamed pause and resume to stop and start in version 5.**
  Trackfetch tries the new names and falls back to the old ones, so both work.
- **JDownloader is reached through My.JDownloader**, which needs an account and
  an internet connection even when JDownloader is on the same computer. The
  older local-only API is deprecated and switched off by default, so it was not
  used.
- **The installer is not code signed.** Windows SmartScreen will show a warning
  the first time. Getting rid of that requires a paid certificate.

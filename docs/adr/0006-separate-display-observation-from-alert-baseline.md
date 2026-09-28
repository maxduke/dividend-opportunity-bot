# Separate the displayed observation from the automatic alert baseline

Manual queries and daily briefings can evaluate a rule between two intraday
monitor runs. They update the latest score shown to the user, but they must not
consume the threshold crossing or level upgrade that the monitor would alert on.

Each rule therefore stores two score/level pairs. `last_score` and `last_level`
are the latest displayed observation. `last_monitor_score` and
`last_monitor_level` are the prior successful intraday monitor observation used
to decide whether the next monitor result crosses the rule's threshold or
upgrades its level. Only the intraday monitor advances that pair. A failed alert
send advances neither pair, allowing a later monitor run to retry. Cooldown and
the highest level alerted today remain separate notification history.

Creating or resuming a rule sets both pairs from its initial snapshot without
sending an alert. Existing databases copy the former `last_score` and
`last_level` into the new monitor pair once during migration. Future migrations
leave the monitor pair intact even if a manual query changes the display pair.
The displayed pair also has `last_observed_at`, so a slow older evaluation
cannot replace a newer displayed score.

# Use a per-rule alert threshold and an initial baseline

`/addop` stores `min_score` as the rule's own alert threshold, defaulting to the global threshold when omitted. Creating or re-enabling a rule evaluates and stores the current snapshot as its baseline without sending an alert; later notifications require a threshold crossing or a level upgrade. This avoids duplicate “creation” alerts while keeping different asset—benchmark rules independently configurable.

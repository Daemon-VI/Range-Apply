"""Blueprint Phase 10 — verification, Signal Inbox and outcome attribution.

Three things are kept apart on purpose:

* the **application lifecycle** (``applications.status``, Phase 6);
* a **signal**: one externally observed piece of information (an execution
  result, an employer email, a status page, a manual note);
* an **outcome event**: what a signal, once classified and attributed, says
  happened to one application — append-only, with evidence strength.

Everything is deterministic first and works with AI completely disabled;
the Phase 8b gateway is the only optional route (``CLASSIFY_SIGNAL``). No
signal, and no model, ever writes a candidate fact into the Evidence Graph.
"""



PRE-COMMIT TO THE OUTCOME. Before you finish, state in your plan what a SUCCESSFUL build looks like for THIS change: which module(s) must compile and which behaviour the change must produce. Committing to that up front makes it much harder to rationalize a failing build as 'someone else's problem' — if verify then reports a failure your expectation did NOT predict, treat it as a signal that your change OR your mental model is wrong (re-read the real code), not as noise to wave past or 'fix' by adding dependencies.

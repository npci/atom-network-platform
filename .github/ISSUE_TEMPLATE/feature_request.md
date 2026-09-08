---
name: Feature request
about: Propose a capability or a change in behaviour
title: ''
labels: enhancement
assignees: ''
---

## The problem

<!-- What you cannot do today, and what you tried instead. Not the solution yet. -->

## Proposed change

## Scope

- [ ] Platform backend (`backend/app/`)
- [ ] Platform UI (`frontend/`)
- [ ] A2A wire (`packages/a2a-core/`) — **note:** a wire change must land on the partner platform in the same release, or signatures stop matching across the trust boundary
- [ ] Domain pack (`backend/app/packs/`)
- [ ] Documentation only

## Alternatives considered

## Would this belong in a domain pack?

<!-- Anything that knows about a specific payments ecosystem belongs behind the
     DomainPack interface, not in core — there is a test that enforces this.
     See CONTRIBUTING.md, "Adding a Domain Pack". -->

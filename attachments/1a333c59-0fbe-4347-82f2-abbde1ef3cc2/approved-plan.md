# Shared camera-to-answer engine and Stowfit

## Summary

Extract a reusable engine into `shared/engines/camera-to-answer`, migrate Package Sizer onto it, and build **Stowfit**,
an English-language iPhone app for checking bags against airline allowances.

Keep `template/` as the generic scaffold. Stowfit starts from it and composes the shared engine with its own luggage
domain, branding, billing, and airline rules.

Deliver Stowfit through **owner-only internal TestFlight**, including its backend, icon, logo, copy, marketing assets,
billing plan, and model evaluation. Package Sizer receives the architectural migration and regression verification; its
pending store submission remains separate.

## Implementation

### Shared engine and Package Sizer migration

Create three modules:

| Module            | Shared responsibilities                                                                                                               | Product-owned inputs                                                  |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Swift core        | Bounded transport, cancellation and stale-response protection, typed analysis state, protected photo/history storage                  | Request/response adapters, record types, storage identity             |
| SwiftUI/iOS       | Camera/import, image normalization, advisory preflight, processing/cancellation presentation, reusable history and purchase mechanics | Guidance, theme, strings, result views, entitlement policy            |
| TypeScript Worker | Image validation, Effect request lifecycle, deadlines, Apple verification, atomic allowance mechanics, safe errors                    | Schemas, prompts, model configuration, quotas, routes and identifiers |

Use composition through typed adapters and SwiftUI view builders. Keep product domain targets independent of platform
code. Shared modules must contain no product-name switches or airline/shipping semantics.

Preserve Package Sizer’s existing API envelopes, `needs_reference` behavior, prompts, model/gate configuration, quota
keys, purchases—including retired lifetime entitlements—and existing history/photo files. Keep its shipping result and
PDF report product-owned.

Register the shared packages and Stowfit with workspace tooling, Just recipes, native runtime, and Worker release
tooling. Document how another camera-to-answer product supplies its adapters.

### Stowfit experience and airline rules

- Identity: `stowfit`, bundle `com.prberg.stowfit`, backend `app-stowfit.prberg.com`. Name screening found no exact
  store match in the checked US/UK/RO storefronts and no
  [TMview results](https://www.tmdn.org/tmview/#/tmview/results?page=1&pageSize=30&criteria=C&basicSearch=Stowfit);
  confirm availability when creating the Apple record.
- Flow: **choose airline and bag allowance → photograph/import or enter dimensions → inspect fit → correct measurements
  or compare another airline**. Save the last 30 checks locally, retaining the photo, measurement provenance, and policy
  version.
- Result hierarchy: dimension verdict first, then estimated/measured dimensions against limits, weight status, fare
  conditions, and official source. Photo results say **“Likely within size limit,” “Near the limit,” “Likely
  oversized,”** or **“Measurements needed.”** Measured results say **“Within size limit”** or **“Exceeds size limit.”**
- Never infer weight, fare entitlement, compression of soft bags, or airline acceptance from a photo. Optional measured
  dimensions and weight work offline. Comparing airlines does not consume another photo attempt.
- Ship 20 airlines: Ryanair, Wizz Air, easyJet, British Airways, Lufthansa, Eurowings, Air France, KLM, Vueling,
  Transavia, TAP, Iberia, Norwegian, SAS, Turkish Airlines, American, Delta, United, Southwest, and JetBlue.
- Maintain one versioned JSON catalog, bundled into iOS and served through `GET /api/airlines`. Records contain official
  source links, review dates, measurement conventions, dimensions, weight/combined-weight rules, allowance conditions,
  and unresolved exceptions.
- Refresh the catalog on foreground with ETag caching; retain it offline. After 30 days without verification, label
  comparisons as using older rules and require checking the official source before relying on them.
- Compare unrounded measurements across permitted orientations, including linear-size restrictions. Photo verdicts use
  the full uncertainty intervals: entirely inside gives “likely within,” entirely outside gives “likely oversized,”
  overlap gives “near the limit.”
- Preserve airline-specific conventions. For example,
  [Wizz Air’s wheel/handle exception](https://ssr-weu2.wizzair.com/en-gb/help-centre/booking-information-and-services/baggage/baggage-allowance)
  requires suitable body measurements; never subtract an assumed wheel height. Unknown numeric limits produce an
  unresolved comparison.
- Include a clearly labeled offline sample and a shareable report whose preview, caveats, measurements, and rule sources
  match the saved result.

### Billing and model

Apply `$billing-plan stowfit` after scaffolding, using the selected commercial terms:

| Tier         |       Price | Photo allowance                          |
| ------------ | ----------: | ---------------------------------------- |
| Free         |        Free | Three admitted attempts per installation |
| Plus monthly | $2.99/month | 10 per UTC calendar month                |
| Plus annual  | $14.99/year | 10 per UTC calendar month                |

Manual checks, airline browsing, comparisons, saved results, sharing, and support remain free. Attempts admitted to
inference count even if cancelled or unsuccessful; malformed requests do not. Paid allowances are shared across restored
installations, do not stack with free allowances, and do not roll over.

Use `com.prberg.stowfit.plus.monthly` and `.annual`, with separate product namespaces and authoritative Apple
verification. No trial, weekly plan, lifetime offer, or launch retention discount. Cancellation is expected for
occasional travelers; record that rationale and economics for every target currency. Verify Apple’s equalized GBP, EUR,
and RON prices before configuration.

Run `$product-llm-finder stowfit` against the completed contract:

- Default: **`google/gemini-3.8-flash`, medium thinking**, one structured vision call for bag recognition, observations,
  dimensions, uncertainty, and abstention.
- Keep airline rules and fit computation outside the model. Do not reuse Package Sizer’s shipping-package classifier.
- Pin EU inference using Gateway’s
  [regional routing](https://vercel.com/changelog/regional-inference-now-available-on-ai-gateway), and verify the
  returned region.
- Compare against `openai/gpt-6.1-sol`; assess Luna and Gemini 4 Carbon as documented alternatives. Select Sol only if
  it materially improves the fixed luggage evaluation.
- At 6,000 input and 1,500 billed output tokens, the current EU Flash estimate is about
  **$0.011/request**. Stress billing at **$0.05/attempt**, including calendar-boundary exposure; replace assumptions
  with measured evaluation costs.
- Preserve Package Sizer’s model selection during extraction.

### Brand and marketing

Use a compact carry-on silhouette nested inside an overhead-bin outline, warm orange against deep navy, with cream
surfaces and restrained status colors. Generate the icon and logo with `$imagegen`; retain source assets and generation
briefs.

Core copy: **“Check your bag before you fly.”** Store subtitle: **“Carry-on size checker.”** Keep uncertainty beside
photo results and avoid guaranteed acceptance or fee-saving claims.

Produce the English listing draft, four genuine-app screenshot compositions, a small press kit, and a
`$marketing-research` plan covering launch channels, travel creators, communities, and proposed Meta/TikTok audiences.
Marketing remains drafted; posting and ad spending are outside this delivery.

## Verification and TestFlight

- **Engine regression:** image orientation/metadata/size bounds; cancellation and stale completions; protected history
  recovery; transport bounds; Apple proof validation; atomic quotas; cross-product entitlement isolation.
- **Package Sizer:** existing Swift/server tests, app build, workerd/boot checks, old history fixtures, and
  representative capture → estimate → history → share verification.
- **Stowfit rules:** exact boundaries, rotations, imperial conversion, uncertainty overlap, linear limits, missing
  dimensions, combined weight, fare ambiguity, wheel exceptions, and stale/offline catalogs.
- **Model evaluation:** fixed positive luggage photos with documented exterior dimensions, ambiguous-scale cases, poor
  photos, and non-bags; two runs per candidate. Preserve failures and distinguish manufacturer dimensions from
  independently measured specimens. Require valid contracts, conservative abstention, no false “likely within” results
  on known oversized cases, and useful estimates on at least half the eligible positive cases.
- **Native behavior:** camera/import, permission denial, manual editing and keyboard dismissal, cancellation, offline
  use, saved-result edits, sharing, purchase failure/pending/cancellation/restore, VoiceOver, and reduced motion.
- **Release:** complete the new app’s TestFlight device matrix, legal publication, sandbox purchase checks, diagnostics
  and matching symbols. Archive the committed source, deploy the required Worker bundle, upload version **1.0.0** with
  the next available build number, and verify owner internal-testing availability.
- Prepare concrete provider/store configurations before requesting any remaining provisioning, purchase-product, or
  account-holder declarations required by the applicable workflows. Do not report a blocked gate as passed.

## Orchestration

- Workers: **Codex — host default**.
- Research: **R1–R3 completed**: native seams, server/scaffolding contracts, and 20-airline policy evidence; United’s
  rendered policy was verified by the parent.
- Companion skills: parent owns architecture, billing/model decisions, design direction, and release actions;
  implementation briefs include the relevant skill requirements.
- Strategy: **hybrid**, at most three workers concurrently.
- Agents: **8**, separating native/server extraction, product implementation, commercial assets, marketing, and
  aggregate verification.
- Validation owner: **A7**, running aggregate checks once after integration. Parent owns deployments and store actions.

| Agent | Wave | Depends on             | Scope                                                   | Model       | Effort | Implementation brief                                                            | Completion evidence                                  |
| ----- | ---- | ---------------------- | ------------------------------------------------------- | ----------- | ------ | ------------------------------------------------------------------------------- | ---------------------------------------------------- |
| A1    | 1–2  | none                   | Shared Swift engine; Package Sizer iOS                  | gpt-6.1-sol | xhigh  | Extract primitives, then migrate workflow/UI in two bounded briefs              | Scoped tests; preserved storage and request behavior |
| A2    | 1–2  | none                   | Shared server engine; Package Sizer server              | gpt-6.1-sol | xhigh  | Extract runtime mechanics, then migrate adapter                                 | Contract, quota and workerd tests                    |
| A3    | 1–2  | none                   | Scaffold, registrations, airline catalog and fit domain | gpt-6.1-sol | high   | Scaffold first; implement sourced catalog and deterministic comparisons second  | Metadata/recipe tests; rule fixtures                 |
| A4    | 3    | A2, A3                 | Stowfit server and evals                                | gpt-6.1-sol | medium | Implement luggage adapter, billing enforcement and eval harness                 | Server tests; bounded production eval results        |
| A5    | 3    | A1, A3                 | Stowfit iOS integration and views                       | gpt-6.1-sol | xhigh  | Implement the specified flow, purchases and report                              | Native tests/build; focused UI verification          |
| A6    | 3    | A3                     | Branding, billing/legal docs and icon assets            | gpt-6.1-sol | high   | Complete identity and commercial assets; keep asset scope disjoint from A5      | Reviewed assets; sourced billing economics           |
| A7    | 4–5  | A1–A6                  | Integration and release preparation                     | gpt-6.1-sol | xhigh  | Integrate and run aggregate checks; then complete matrix/gate evidence          | Both products pass; archive inputs and gate reports  |
| A8    | 4, 6 | A4–A6; A7 for captures | Marketing and listing assets                            | gpt-6.1-sol | high   | Research channels first; compose final screenshots/copy after verified captures | Verified sources; truthful listing and exports       |

Configurations are Jev-selected. Split work into roughly 15–30-minute briefs at the indicated dependency boundaries;
estimates are not stop conditions.

- Code polish: **required**, once across the final changed-code scope.
- Agent-context maintenance: **required**, once for affected contracts and directory guidance.

## Wait out conflicting agents

Named scope draft recorded: `camera-answer-stowfit`.

Promote before implementation:

```sh
ai-coord start --draft camera-answer-stowfit
```

Only if that draft is missing, use:

```sh
ai-coord start 'Camera-to-answer engine and Stowfit TestFlight' \
  AGENTS.md shared/AGENTS.md package.json bun.lock justfile \
  scripts/apple-runtime.ts scripts/apple-runtime.test.ts scripts/just.test.ts \
  shared/cloudflare/worker-releases.ts shared/cloudflare/worker-releases.test.ts \
  products/package-sizer/AGENTS.md products/package-sizer/package.json \
  products/package-sizer/justfile products/package-sizer/tsconfig.json \
  products/package-sizer/vitest.config.ts products/package-sizer/docs/contract.md \
  products/package-sizer/docs/reference.md products/package-sizer/docs/ios-reference.md \
  products/package-sizer/docs/backend.md \
  --recursive shared/engines/camera-to-answer \
  --recursive products/package-sizer/apps/ios \
  --recursive products/package-sizer/apps/server \
  --recursive products/stowfit
```

Require `READY`; if blocked, use `ai-coord wait`, handle peer messages, and retry the same complete claim.

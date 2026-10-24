# Campaign button analytics

`campaignButtonAnalytics(campaignId, buttonKey?, offset=0, limit=50)` requires
`SEND_CAMPAIGNS`. It returns summary counts, button groups and a page of individual
clicks (maximum 200). Summary counts also exist on `campaignMetrics` and
`campaignMetricsBatch`, using the same calculation.

An interaction requires an inbound message referencing the exact outbound Meta
message ID on a campaign recipient or any historical send attempt. The sender's
normalized phone must match the destination (Mexican 52/521 aliases collapse).
Duplicate provider message IDs count once; conflicting evidence is excluded.
Missing or ambiguous references are not attributed using the most recent campaign.

Template quick-reply callbacks (`message_type=button`) count as clicks. Legacy
`interactive` messages require the raw webhook to prove `button_reply`; list
selections do not count. Quoted manual text, even an identical button caption,
counts separately as a written reply. Unquoted messages have no exact campaign
attribution and do not enter these written-reply counts. Clicks never modify
delivery/read receipts or send state.

Groups use normalized actual captions and include current campaign/variant template
buttons with zero clicks. Old captions remain marked historical after template
edits. People are deduplicated by phone within each group and across the summary;
group unique counts must not be added. `clicks`, `uniquePeople`, `convertedPeople`
are null for URL, phone, flow and other non-quick-reply buttons: no individual click
callback exists for these buttons in our data. Null means unmeasured, not zero.

Conversions require an existing campaign recipient attribution plus a completed
payment belonging to that person. Payment and conversion times must both follow
the click. This is subsequent campaign attribution, not proof that a button caused
the sale. Refunded/pending payments are excluded. One person can appear in multiple
button groups but only once in `convertedClickers`.

All timestamps are UTC; the desktop renders Mexico City local time. Analytics are
read-only and reconstruct available history immediately, without migrations,
backfills, customer messages or campaign recoveries. Retention of messages/raw
webhooks limits available historical evidence.

Deploy backend before desktop: the new client selects additive GraphQL fields.
Refresh a completed campaign with **Actualizar interacciones**. Active campaigns
refresh alongside the existing live metrics timer. Validate with
`pytest tests/test_campaign_button_metrics.py -q` on an ephemeral PostgreSQL 16 DB.

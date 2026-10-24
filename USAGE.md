# Orbyte Collective — User Guide

Orbyte Collective helps you review supply records, compare delivered prices, buy together with nearby businesses, and check invoices. You stay in control: **review records, accept useful rules, and approve orders as separate actions.**

This guide follows **Aina, a café owner who needs to restock paper cups**. Button names below use the English interface. You can switch to **Bahasa Melayu** or **中文** at the top of the page.

## Before you begin

Ask your demo organiser for the app link and an assigned owner account. There is no public account-registration flow. For a local installation, follow the [setup instructions](README.md), then open [Orbyte Collective](http://127.0.0.1:3000).

The hackathon demo uses synthetic café data. Purchase orders and supplier messages are simulated: **no real supplier receives an order or message, and no payment is made.**

In the default replay mode, the app reads only prepared examples with matching cached responses. A new photo or a changed message may stop with “No exact replay is available.” Ask the organiser for a supported example or a configured live-reading session. Repeatedly uploading the same unsupported input will not make it readable.

For organisers: local demo credentials are generated in `.cache/demo-demo.json` after seeding. Keep that file private. The seeded cafés already have purchasing needs and quotes, so users can start at **step 4** for the shortest buying walkthrough. The upload and learning sections are additional journeys; do not expect every optional action to have a replay in every demo session.

## Find your way around

| Screen | What you do here |
| --- | --- |
| **Inbox** | Upload sources; review records, product matches and supplier quotes; revisit supplier messages. |
| **Your next order** | Choose quantities, compare prices, request supplier prices, find pools and approve orders. |
| **Ledger** | Browse confirmed purchases, stock observations, quotes and simulated POs; check the self-audit summary. |
| **Learned rules** | Review evidence-based usage, delivery-fee and quote-accuracy proposals. |
| **Activity** | Follow recorded actions and background processing. |

## 1. Sign in and choose your café

*Aina starts her weekly restocking review.*

1. Enter your **Email** and **Password**, then select **Sign in**.
2. Check that your café appears under **Your workspaces**, with the **Owner** role.
3. Select **Open workspace**.
4. If you have several businesses, use the **Business** selector to choose the one you are working on.

**You should see:** your café's purchasing workspace and the five navigation links. Check the business before uploading a source or approving anything.

If no workspace is assigned, ask the organiser to assign one. Use **Account → Sign out** before signing in as another café owner. The Business selector only switches between businesses your account can access.

## 2. Add a source and check what the app may read

*Aina has an old cup invoice and a note about the cups still on her shelf.*

1. Open **Inbox**.
2. Under **Sources**, choose a **Source file**, or paste into **Or paste invoice, stock or supplier text**. Use one input at a time.
3. Select **Upload source**.
4. In **Source viewer**, inspect the original. Mask sensitive image/PDF regions with **Add mask region**, or enter sensitive text values, as needed. Keep the purchasing information needed for review readable.
5. Tick the source-masking review checkbox, then select **Prepare reviewed source**.
6. Wait for reading to finish. Inspect the prepared copy with **Show prepared copy** when available.

Supported uploads are PNG, JPEG or PDF up to **10 MB and 10 pages**, or UTF-8 text up to **16,000 characters**. The repository includes synthetic examples such as `fixtures/sources/single-business-invoice.txt`, `stock-ms.txt`, and `delivered-invoice.png`; the organiser should identify which examples match the current demo session.

**You should see:** a proposed reading, or an explicit message explaining why reading stopped. Uploading and preparing a source do not confirm a purchase or approve an order.

## 3. Turn the reading into a checked record

*The app proposes the invoice details. Aina compares them with the source before adding them to her history.*

1. In **Record review**, inspect the supplier, date, quantities, units, prices, delivery charge and total. Open the source evidence for uncertain fields.
2. Select **Edit record** to correct anything the source supports. Leave unknown values blank; do not enter zero just to make the form complete.
3. Choose the supplier and purchase purpose: **Historical purchase** for an earlier purchase, or **Delivered invoice** for an invoice to check against an issued order.
4. Select each item's **Confirmed item mapping**. This connects the supplier's product name and sale unit to the product you actually buy.
5. Select **Save corrections**, review the remaining flags, and tick the source-review acknowledgement.
6. Select **Confirm record**. If the proposal should not be used, select **Reject proposal** instead.

**If the product is unmapped:** the owner can use **Match a product specification → Request matching**. Check capacity, material, lid diameter, sale unit and pack factors. For example, enter `20 × 50` only when the source confirms 20 packs of 50 pieces. Tick the acknowledgement and select **Confirm product mapping**. Then edit the record and select that confirmed mapping before confirming the record. A similar name is not proof that two products are interchangeable.

For a stock note, check the observation type, actual count, unit and observation time. A stock count, received stock and waste describe different events. **Confirming an invoice does not confirm that stock arrived.** Record actual arrival separately when appropriate.

**You should see:** **Record confirmed**, with a record reference. The record is now available in **Ledger**. Incomplete or unmapped data may still be excluded from comparisons.

## 4. Choose how many cups to buy

*Aina opens her next order and checks whether 1,000 cups will cover her need.*

1. Open **Your next order** and find the cup need.
2. Review **Estimated run-out**, the daily usage basis, and **Quantity and cash**.
3. Compare available quantity options, including coverage, **Extra cash now**, and any **Saving against repeat orders**. Read the stated comparison assumptions.
4. Select **Select quantity** for the option you want, or **Keep stated quantity** when offered.

**You should see:** **Selected · quantity fixed**. Selection is preparation; it creates no order. A pooled order uses your selected need rather than increasing your quantity to help the group reach a tier.

If no needs or valid options appear, ask the organiser to check the business's configured need, stock, usage and supplier terms. The current walkthrough starts from an assigned, configured business; account setup and product-catalogue administration are not part of this user flow.

## 5. Compare suppliers or request a price

*Aina wants to compare the amount she will actually pay, including delivery.*

On **Your next order**, review the compatible supplier offers:

- **Agreed total** is based on the confirmed supplier agreement.
- **Expected total** may include an accepted estimate based on invoice history.
- **Decision total** is the amount used for purchasing checks and your approval cap.
- **Comparable historical cost** and **Best current solo cost** give separate comparison points. Check **Credit**, delivery window and conditions alongside the amount.

If you need another price, choose **Supplier to ask** under **Supplier messages**, then select **Prepare price request**. Review the exact message and recipient, save any edits, tick the acknowledgement, and select **Send simulated request**. Repeat for another supplier if needed. Sending a request does not approve a purchase.

To review a reply, upload and prepare it through **Inbox**. In **Supplier quote review**, choose **Supplier for this quote** and **Product need**, then select **Read supplier quote**. Check tiers, minimum quantities, fees, tax, credit, validity, delivery dates and pooling permission. Save corrections and acknowledge the terms before confirming the quote.

**You should see:** confirmed, complete quotes become available to the purchasing calculation. Missing delivery terms or expired quotes may make an offer unavailable. Do not assume that sending a simulated price request will produce a real supplier reply.

## 6. Buy alone or look for a pool

*Aina can buy immediately on her own, or check whether nearby cafés make her own order cheaper.*

**Buying alone:** choose **Review solo order** on a compatible offer, then continue to step 7.

**Buying together:** under **Shared buying option**, select **Find a pool**. Review **Your pooled decision cost**, **Your saving versus solo**, your delivery window and the offer deadline. Every participating owner must approve their own order.

**You should see:** an eligible offer or **No eligible pool**. No pool is a valid result; you can continue with solo quotes. The app does not show another café's private prices.

## 7. Approve exact terms, then create the order

*Aina is happy with the supplier, quantity, delivery and price limit.*

1. Review the displayed supplier and product terms under **Your bound approval**.
2. Enter **Maximum delivered cost (RM)**. It must cover the current decision total.
3. Set **Approval expiry (Malaysia time, UTC+8)** after now and no later than the permitted deadline.
4. Select **Approve these terms**.
5. Check the saved cap, expiry and approval status.
6. For a solo order, select **Create simulated PO** when available. For a pool, wait for the required approvals, then select **Create simulated POs** when available.

**You should see:** **Order issued**, an order reference and a frozen set of issued terms. A pool produces a separate PO for each business. Approval alone does not issue a PO, and a simulated PO does not contact a supplier.

### Story branch: a café withdraws

In the synthetic three-café example, each café orders 1,000 cups. The initial pooled cost is **RM255 per café**, compared with a **RM315** solo baseline. If Café C withdraws, the remaining pooled cost becomes **RM285**.

An approval capped at **RM276** cannot cover RM285. Its owner sees **Your previous approval is void. Fresh consent is required.** Review the new terms and choose **Give fresh approval** with a sufficient cap, or **Decline / withdraw from pool**. An approval may stand if the revised cost remains within its cap and all other bound terms remain valid.

These figures illustrate the declared synthetic scenario. Read the current screen: dates, accepted rules and prior actions can change availability and amounts. Once a PO is issued, this pre-release withdrawal flow no longer applies.

## 8. Check the delivered invoice

*Aina's supplier invoice says RM290, but her issued PO agreed RM285. She wants to understand the RM5 difference.*

1. Open **Inbox**, upload the delivered invoice, prepare it, and review it as in steps 2–3.
2. Set **Purchase purpose** to **Delivered invoice**, verify the supplier and item mappings, and **Confirm record**.
3. In the invoice-check section beneath the confirmed record, select **Issued PO for this invoice** and choose **Check invoice against selected PO**. A check may already have been queued after confirmation; review the saved result and selected PO.
4. Inspect the total variance and the item/fee differences. The component differences overlap, so do not add them together.
5. If the discrepancy is supported, enter the **Owner-confirmed discrepancy amount (RM)**, tick the review acknowledgement, and select **Confirm discrepancy**. Reject an unsupported suspicion with **Reject suspected discrepancy**.
6. If needed, select **Prepare supplier query**, inspect and save its exact text and recipient, acknowledge the saved version, then select **Send simulated query**.

**You should see:** a passed, flagged or unmatched check. **Unmatched · not passed** means the app lacks a usable matching PO or required comparison information; it is not a clean bill of health.

### Record a correction only when you have evidence

If the supplier supplies a credit note or corrected invoice, upload and prepare that **separate document** in Inbox. Reopen the original delivered invoice with **View source**, return to its invoice check, select the reviewed correction/credit source, document type and reference, and enter the supported recovery amount. Tick the proof acknowledgement and select **Record documented recovery**.

**Flagged**, **confirmed discrepancy**, and **documented recovery** are different outcomes. Sending a query does not mean money was recovered.

## 9. Review what the app has learned

*After several comparable records, Aina can review a suggested delivery fee or usage rate.*

1. Open **Learned rules**. If needed, select **Generate rule proposals**.
2. Read the sample count, period, range, exclusions and supporting sources. A fee proposal needs at least three comparable invoices before acceptance.
3. To apply a supported proposal, tick the evidence acknowledgement and select **Accept rule**. Otherwise select **Reject proposal**.
4. Use **Revoke accepted rule** if you no longer want that rule applied.

**You should see:** **Accepted · active** only after explicit acceptance. Usage rules affect replenishment; observed-fee rules affect decision estimates; quote-accuracy rules add historical warnings. A learned fee does not rewrite an issued PO's agreement. Recheck open approvals after a rule changes: they may become void.

Network administrators review historical aggregate rules in their assigned network workspace. These need at least **five distinct businesses**; the three-café demo cannot activate a network rule by itself. Individual business evidence remains private.

## 10. Finish with your records and activity

*Aina checks what was actually recorded before closing the app.*

In **Ledger**, use **Record type** to browse purchases, stock observations, confirmed quotes and simulated purchase orders. Use **View source** where available. The **Self-audit** card separates delivered invoices, complete checks, mismatches, confirmed variances and documented recoveries.

In **Activity**, review recorded actions and **Background tasks**. A completed background task means processing finished; it does not mean a record, rule or order was approved.

## When something stops you

| What you see | What to do |
| --- | --- |
| No exact replay available | Ask the organiser for the supported source/context or a live-reading session. No live call is made automatically. |
| Missing or unreadable fields | Verify the source, save supported corrections and review the flags. Leave unknown values blank. |
| Unmapped item | Ask the owner to confirm the correct specification and pack size, then select that mapping on the record. |
| No comparable baseline or unavailable quote | Review confirmed purchase history, mappings and quote completeness/validity. Do not infer savings from an incomplete comparison. |
| No eligible pool | Continue with a compatible solo offer, or review the unmet requirements. |
| Void, expired or stale approval | Refresh and review the current terms. Give fresh approval if you still agree; an old permission is not restored automatically. |
| Staff action blocked or prices hidden | Ask the owner to perform the restricted review. Staff permissions depend on the business's configured limits. |
| Processing queued or needs review | Check Activity for status. Use the relevant retry/refresh control after the cause is resolved; avoid creating repeated uploads. |
| Connection unavailable | Reconnect to the service. Disconnected browser changes are not queued. |

If **Install Orbyte** appears, use it to open the installation prompt. Installation depends on browser support and does not make private records or purchasing actions available without the service connection. A locally hosted replay demo can run without external internet only when its local services remain running.

Your weekly routine is: **review sources → choose a quantity → compare offers → approve terms → create the simulated PO → check the invoice**. Use learned rules and pooling when they are available and useful for your own business.

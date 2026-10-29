# Synthetic explainability example

Declared evidence comparison, not a hiring prediction or verified competence judgment

Mandatory-rule decision: `needs_review`.

## Python at intermediate level

{"requirement_id":"0cb67d6f-05e2-5ab2-8d32-abbd6d834664","importance":"hard","outcome":"met","reason":"supported","evidence_ids":["87bc9135-232d-5315-b158-5b8e7e9fb33d"]}

The declared profile evidence supports the configured requirement rule.

Linked profile facts: 7aee620d-06a8-5e0f-afbd-a3e2cef01f0d, ea7fc561-9592-55e5-903b-b2e70dad84d4

## Work authorization explicitly declared

{"requirement_id":"ba0d0a60-0570-51b0-acd0-c4557b97be3a","importance":"hard","outcome":"met","reason":"supported","evidence_ids":["87bc9135-232d-5315-b158-5b8e7e9fb33d"]}

The declared profile evidence supports the configured requirement rule.

Linked profile facts: fd2c44c0-69cf-51e4-ac42-2489edd9599f

## English B2 preferred

{"requirement_id":"1f426d0f-5d32-5656-a736-6a72cde1eee0","importance":"soft","outcome":"met","reason":"supported","evidence_ids":["87bc9135-232d-5315-b158-5b8e7e9fb33d"]}

The declared profile evidence supports the configured requirement rule.

Linked profile facts: bb2d47ce-2589-5f5a-ba48-fd670d4f4fc5, b00b573a-aa11-5466-9e1a-2c68df21dbd9

## SQL preferred

{"requirement_id":"da2a4ac6-1fc9-5bc0-918a-17f7f4b455f2","importance":"soft","outcome":"unknown","reason":"unverified","evidence_ids":[]}

Available claims are unverified under the assessment evidence policy.

Linked profile facts: 7c86daf6-3d87-5e47-af79-a414f3e8d29e, 3c014ff4-7726-5251-9239-9f70cfe5b3e1

## German B2 preferred

{"requirement_id":"e3a5aecc-f089-52d9-8bc0-8d3c9914eaac","importance":"soft","outcome":"unmet","reason":"below_minimum","evidence_ids":["87bc9135-232d-5315-b158-5b8e7e9fb33d"]}

The evidenced declared level is below the configured minimum; this is a stated-data gap, not a competence judgment.

Linked profile facts: 533d5c36-f06c-5cdd-af52-f85f15e5eaff, 7723c68e-3ff3-5a3d-9ad6-a2fa2efb94d2

## Leadership potential (unspecified importance)

{"requirement_id":"e38308c9-cddc-5b67-9330-105d4f179f11","importance":"unspecified","outcome":"unknown","reason":"unsupported_criterion","evidence_ids":[]}

This criterion is outside supported business rules. No candidate judgment is made.

Linked profile facts: 

## Fact provenance

- `b96c6991-4539-5b1c-ab21-c6ac797514c0` — user_confirmed; `display_name`: Synthetic Candidate. Evidence: user_statement: fixture:synthetic-user-statement
- `7aee620d-06a8-5e0f-afbd-a3e2cef01f0d` — user_confirmed; `skills/f4d48804-dd0b-56e0-b976-23aadc00ced7/name`: Skill: Python programming. Evidence: user_statement: fixture:synthetic-user-statement
- `ea7fc561-9592-55e5-903b-b2e70dad84d4` — user_confirmed; `skills/f4d48804-dd0b-56e0-b976-23aadc00ced7/proficiency`: Python programming proficiency: advanced. Evidence: user_statement: fixture:synthetic-user-statement
- `7c86daf6-3d87-5e47-af79-a414f3e8d29e` — unverified; `skills/ef008422-09b6-598b-884e-93ed2635a0ce/name`: Skill: SQL. Evidence: ai_inference: fixture:unreviewed-inference
- `3c014ff4-7726-5251-9239-9f70cfe5b3e1` — unverified; `skills/ef008422-09b6-598b-884e-93ed2635a0ce/proficiency`: SQL proficiency: expert. Evidence: ai_inference: fixture:unreviewed-inference
- `bb2d47ce-2589-5f5a-ba48-fd670d4f4fc5` — user_confirmed; `languages/ddd45210-832f-52cb-a216-45a682462833/name`: Language: English. Evidence: user_statement: fixture:synthetic-user-statement
- `b00b573a-aa11-5466-9e1a-2c68df21dbd9` — user_confirmed; `languages/ddd45210-832f-52cb-a216-45a682462833/proficiency`: English level: C1. Evidence: user_statement: fixture:synthetic-user-statement
- `533d5c36-f06c-5cdd-af52-f85f15e5eaff` — user_confirmed; `languages/acc18ac7-859e-5d57-b046-0c5bd2f23240/name`: Language: German. Evidence: user_statement: fixture:synthetic-user-statement
- `7723c68e-3ff3-5a3d-9ad6-a2fa2efb94d2` — user_confirmed; `languages/acc18ac7-859e-5d57-b046-0c5bd2f23240/proficiency`: German level: A2. Evidence: user_statement: fixture:synthetic-user-statement
- `d0bbc415-00a6-5d41-a906-d7ab78dd4aa6` — user_confirmed; `experience/ebea4c57-1cac-5274-903e-97d73a4b8b2e`: Software Developer | Example Labs | 2021-01-01 to 2024-01-01. Evidence: user_statement: fixture:synthetic-user-statement
- `42080da8-d701-535c-8553-27f715d2f7c1` — user_confirmed; `education/9c436378-b8e1-525d-a0ff-109855d08366`: Computing degree | Example Institute | 2017-01-01 to 2020-01-01. Evidence: user_statement: fixture:synthetic-user-statement
- `e55312d7-6ab8-5aed-b2f0-b2fa86c3ddbc` — user_confirmed; `certifications/2f609b06-40b2-5359-bd10-01392635b6c2`: Example Systems Certificate | Example Training | issued 2022-01-01 | expiry not provided. Evidence: user_statement: fixture:synthetic-user-statement
- `fd2c44c0-69cf-51e4-ac42-2489edd9599f` — user_confirmed; `work_authorized`: Declared work authorization: true. Evidence: user_statement: fixture:synthetic-user-statement

## Clarifications

- Can you confirm the profile statement at skills/ef008422-09b6-598b-884e-93ed2635a0ce/name and supply user-confirmed supporting evidence? (blocking: False)
- Can you confirm the profile statement at skills/ef008422-09b6-598b-884e-93ed2635a0ce/proficiency and supply user-confirmed supporting evidence? (blocking: False)
- For requirement da2a4ac6-1fc9-5bc0-918a-17f7f4b455f2, which current profile fact or user-provided evidence confirms the stated value/level? If unknown, say so. (blocking: False)
- For requirement e3a5aecc-f089-52d9-8bc0-8d3c9914eaac, which current profile fact or user-provided evidence confirms the stated value/level? If unknown, say so. (blocking: False)
- This requirement is outside supported rules. Review whether it is job-related; do not provide protected personal details. (blocking: False)

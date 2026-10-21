# Goaly claims support agent

The language of a support agent that helps insurance callers with their claims while following a fixed procedure.

## Language

**Channel**:
The medium a caller uses to reach the agent: text chat, browser voice, or phone.
_Avoid_: modality, mode, interface

**Interruption**:
The caller speaking while the agent is still speaking, which stops the agent mid-reply.
_Avoid_: barge-in, overlap, talk-over

**Heard reply**:
The part of an agent reply the caller actually heard before an Interruption; only this part counts as said.
_Avoid_: partial reply, truncated reply

**Identity detail**:
One of the five facts a caller gives to prove who they are: full name, date of birth, phone, email, or ID last four.
_Avoid_: PII field, credential, identifier

**Read-back**:
The agent repeating spoken Identity details to the caller and asking if they are right, before checking them; a "no" is not a failed check.
_Avoid_: confirmation, echo

**Placeholder**:
The label the model sees in place of an Identity detail, such as `[DOB given]`; the value itself stays with code.
_Avoid_: token (reserved for the API token), mask, redaction

**Phase**:
One of the four fixed stages of a call (VERIFY_ID, RESOLVE_INTENT, PROCESS_CASE, POST_PROCESS); only code moves a call between them.
_Avoid_: step, state, stage, node

**Slot**:
One named fact the caller has given during the call, such as a claim month, kept with the caller's own words; the current Phase decides whether it may be used.
_Avoid_: memory, variable, parameter, note

**Deferred question**:
A claim question the caller asked before verification, kept as a Slot and answered after verification without asking it again.
_Avoid_: pending question, parked intent

**Policyholder**:
The person a claim belongs to, identified by a party ID in the claims data.
_Avoid_: customer, buyer, member

**Representative**:
A caller acting for a Policyholder, listed by name and relationship for that Policyholder; must pass the Policyholder's identity check and get Consent.
_Avoid_: proxy, agent, third party

**Consent**:
The Policyholder's own approval for a Representative to hear their claim details, requested during the call; while it is pending or never arrives, nothing is disclosed.
_Avoid_: authorization, permission

**Email opt-in**:
The caller's yes or no to a summary email at the end of a call; the email only ever goes to the address on the Policyholder's file.
_Avoid_: consent (reserved for Consent), email permission

### Caller emotions

**Frustration**:
Impatience with the process, such as repeating details already given or pushing to "just tell me".
_Avoid_: annoyance, irritation

**Anger**:
Hostility aimed at the agent or the company, such as insults, swearing, threats, or shouting.
_Avoid_: rage, hostility score

**Anxiety**:
Worry about consequences for the caller, such as money, health, or a deadline.
_Avoid_: stress, fear

**Confusion**:
Not understanding what the agent said or asked, such as asking what a term means or repeating a question already answered.
_Avoid_: misunderstanding

**Refusal**:
Declining a required step, such as withholding identity details or pushing to skip verification.
_Avoid_: pushback, non-compliance

**Emotion reading**:
For one caller message, a strength (none, mild, or strong) for each of the five caller emotions, with the caller's own words that show it.
_Avoid_: sentiment, mood, emotion label

**Mild**:
The emotion is clear in the caller's words, and the caller still answers the agent's questions.
_Avoid_: low, slight

**Strong**:
The emotion is high, or the caller stops answering the agent's questions, or insults the agent.
_Avoid_: high, severe

**Frustration shown**:
Frustration or Anger read as Strong once, or as Mild in two or more caller messages, in the current call.
_Avoid_: escalation flag, frustrated caller

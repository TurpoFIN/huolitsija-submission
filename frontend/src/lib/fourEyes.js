// Four-eyes on customs filing and claim payment (backend routes/admin.py decide): the requester cannot approve their own
// request. "colleague": another user with the permission must approve. "single_user": nobody else can, so the requester
// may approve with an audited override and a reason of at least OVERRIDE_MIN_NOTE characters.
export const OVERRIDE_MIN_NOTE = 10;

const STATE_BY_CODE = { four_eyes_required: "colleague", four_eyes_single_user: "single_user", four_eyes_override_note: "single_user" };

/** "colleague" | "single_user" for a refused decision (ApiError from POST /approvals/{id}/decide), else null. */
export function fourEyesState(error) {
  return STATE_BY_CODE[error?.data?.code] || null;
}

export function overrideReady(note) {
  return String(note || "").trim().length >= OVERRIDE_MIN_NOTE;
}

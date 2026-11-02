"""Refusals a person or an agent can cause, by code."""

USER_ERRORS: dict[str, tuple[str, str]] = {
    "unknownTicket": ("missing", "Unknown ticket"),
    "unknownProblem": ("missing", "Unavailable Eval not found"),
    "unknownTrigger": ("missing", "Unknown trigger"),
    "unknownAssignment": ("missing", "Unknown assignment"),
    "ticketEnded": ("refused", "Ticket has ended"),
    "assignmentEnded": ("refused", "Assignment has ended; a newer assignment owns this ticket"),
    "evalFinished": ("refused", "Eval {eval} has finished or a later eval replaced it; decide the trigger's current eval"),
    "notAdmissible": ("refused", "Trigger is not admissible: {reason}"),
    "notExplicit": ("refused", "A direct accept is for explicit requests; an inferred candidate needs an assessment "
                               "that matched enabled rules, in auto mode"),
    "ticketStopped": ("refused", "A person stopped this ticket; new work needs a new accept"),
    "mandateRevoked": ("refused", "The current policy no longer holds this ticket's mandate; new work needs a new accept"),
    "notRetryable": ("refused", "Only an open ticket whose worker last reported failed or blocked, with no person's "
                                "message since, or whose Input delivery failed, can be retried"),
    "sourceThread": ("refused", "This is the thread the ticket started on; it stays subscribed"),
    "followedThread": ("refused", "This thread is the ticket's own; it stays subscribed"),
    "notFollowing": ("refused", "This ticket does not follow that thread"),
    "questionWithResult": ("refused", "A question for a person cannot also be a ready result or an answer"),
    "askerWithoutQuestion": ("refused", "The message that raised a question needs the question it raised"),
    "notAsker": ("refused", "A question must name the person's message that raised it, which reached this ticket"),
    "notAnswerable": ("refused", "An answer must name the person's question it answers, which reached this ticket"),
    "ackEnded": ("refused", "Ticket ended; do not acknowledge historical requests"),
    "notReached": ("refused", "Trigger does not belong to this worker"),
    "handedBack": ("refused", "This ticket handed that trigger back already"),
    "handedBackHere": ("refused", "The worker of this ticket handed that trigger back as beyond it: accept it as new "
                                  "work, attach it to another ticket, or ignore it"),
    "workerDecides": ("refused", "This message reached ticket {tickets} on its thread: its worker decides whether it "
                                 "belongs there. Only a stop may be routed"),
    "ackNoChannel": ("refused", "Acknowledgement requires a person's source message with a channel"),
    "ackNotPerson": ("refused", "Use the original human trigger, not a bot, CI or worker report"),
    "notReady": ("refused", "Ticket is not ready for delivery; keep progress internal"),
    "notAttention": ("refused", "Attention requires the ticket's current question"),
    "notQuestion": ("refused", "An answer must target the original human question"),
    "noAnswer": ("refused", "No worker answer is ready for this human trigger"),
    "noChannel": ("refused", "No reply channel for this ticket or trigger"),
}


class UserError(Exception):
    """An invalid request: `missing` when it names something that does not exist, else refused."""

    def __init__(self, code: str, values: dict[str, str] | None = None):
        kind, text = USER_ERRORS[code]
        self.code = code
        self.values = values or {}
        self.missing = kind == "missing"
        for name, value in self.values.items():
            text = text.replace("{" + name + "}", value)
        super().__init__(text)

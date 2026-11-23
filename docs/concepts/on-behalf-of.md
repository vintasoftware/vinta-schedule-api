# Acting on Behalf of a Member

An integration often makes changes for a specific person at your customer's
organization: a receptionist books an appointment in your product, and your
product calls our API to create it. With an organization-wide token, our audit
trail would only record that **your integration** made the change.

Send the `X-Public-Api-On-Behalf-Of` header to say which member you are acting
for. Every audit record that names your token as the one who made a change then
also names that member, so the organization can see who was behind it.

```http
POST /graphql/
Authorization: Bearer <token id>:<token>
X-Public-Api-On-Behalf-Of: 4821
Content-Type: application/json
```

The value is the member's user id: the same id the API returns and accepts
elsewhere, for example as `membershipUserId`.

## What it does and does not do

- It is **for the audit trail only**. It records your claim that you acted for
  this member. Your token remains the one that made the change.
- It **does not change what your token can do**. A token keeps exactly the
  access it had, whether or not it names a member.
- We check that the id belongs to an **active member of the request's
  organization**. We cannot check that the member really asked for the change,
  so the record is labeled as a claim made by your token.

## Which requests use it

| Your token | Request | What happens to the header |
|---|---|---|
| Organization-wide | Mutation | Checked and recorded |
| Org-less (sends `X-Public-Api-Organization-Id`) | Mutation | Checked against the organization you chose, and recorded |
| Any | Query | Ignored |
| Scoped to one member | Any | Ignored. That token's member is already recorded |

A request runs one operation. If that operation is a mutation, the header is
checked, and every audit record the mutation writes for your token names the same
member. Records the system writes on its own (for example, follow-up changes it
makes after yours) are not attributed to anyone.

## Errors

When a mutation is rejected, nothing has been changed. The response is a normal
GraphQL error with a code in `extensions.code`:

| Code | Meaning |
|---|---|
| `ON_BEHALF_OF_MEMBER_INVALID` | The header is not a positive whole number, or it does not name an active member of the request's organization. |
| `ON_BEHALF_OF_MEMBER_REQUIRED` | Your token requires the header on mutations, and the request did not send it. |

```json
{
  "data": null,
  "errors": [
    {
      "message": "X-Public-Api-On-Behalf-Of does not name an active member of this organization.",
      "extensions": { "code": "ON_BEHALF_OF_MEMBER_INVALID" }
    }
  ]
}
```

If the header names someone who is no longer an active member, the mutation
fails. Check your member ids when a person leaves the organization.

## Requiring it

An organization admin can make the header mandatory for a token by turning on
**requires on-behalf-of**:

- when creating the token: `requires_on_behalf_of: true` on the REST endpoint, or
  `requiresOnBehalfOf: true` on the `createSystemUserToken` mutation;
- later: `requires_on_behalf_of` on the REST token update endpoint.

With it on, every mutation from that token must name a member, or it fails with
`ON_BEHALF_OF_MEMBER_REQUIRED`. Queries still work without the header. Before
turning it on, make sure your integration sends the header on every mutation.
Every change to this setting is itself recorded in the audit trail.

Tokens scoped to one member cannot turn it on, because they ignore the header.

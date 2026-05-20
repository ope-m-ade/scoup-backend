# SCOUP Admin Guide

## Admin Access

Admin dashboard access requires a Django user with `is_staff=True`.

Admin users can:

-   Review faculty accounts
-   Approve or reject pending faculty
-   Manage faculty visibility
-   Review collaboration inquiries
-   Send messages to faculty
-   Review support tickets
-   Manage contact page content
-   View platform stats and audit logs

## Faculty Review

New faculty users may need:

1.  Institutional email verification
2.  Admin approval
3.  Profile visibility enabled

Only approved and visible faculty should appear publicly.

## Faculty Management

The admin faculty page supports:

-   Search by name, email, or department
-   Filter by active, pending, inactive, or needs action
-   Open side-panel detail view
-   Approve/reject pending profiles
-   Show/hide profiles
-   Export faculty list

## Inquiries

Inquiries include:

-   External visitor inquiries to faculty
-   Faculty-to-faculty collaboration inquiries
-   Project collaboration interest
-   Admin-to-faculty direct messages

Common statuses:

-   `pending`
-   `reviewed`
-   `closed`

Admins can add notes and update status.

## Support Tickets

Support tickets may come from:

-   Public visitors through the floating support form
-   Faculty users through the dashboard

Ticket statuses:

-   `open`
-   `in_progress`
-   `resolved`
-   `closed`

Admins can update status and add internal notes.

## Contact Page

Admins can manage public contact team members and general contact settings through the admin dashboard.

## Data Cleanup Guidance

Treat imported Academic Metrics data as seed/demo evidence, not final product truth. For long-term maintenance, prefer:

-   Faculty submitted data
-   Admin-confirmed school and department records
-   User-managed papers, projects, and patents
-   Admin review notes

Avoid promoting raw affiliation strings into official departments without review.
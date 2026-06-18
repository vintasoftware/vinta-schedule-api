# BuildingBlocks \<-\> Vinta-Schedule Integration Project

The objective of this document is to explain how the integration is going to work and what parts are still missing in the Vinta-Schedule APIs to make sure the integration is covered.

# Integration setup questions

## 1\. How are admins going to connect to vinta-schedule?

### 1.1. create an account in vinta-schedule

You can sign up and create an organization using email/password or social login (Google).

### 1.2. configure the service accounts

Service accounts are necessary to sync room/resource calendars with Google Calendar and do some admin operations on Google Calendar integration.

### 1.3. \[optional\] import resource calendars

You can get your Rooms/Resources from Google Workspace and link them to the events to manage availability.

## 2\. How do admins invite team members to vinta-schedule?

### 2.1. Automatically link vinta-schedule users with Medplum providers

Admins need to create a webhook for the "user created" event in vinta-schedule to notify a medplum bot about the new user, so it can link the identifier with their provider

#### Observation

The "user created" webhook event doesn't exist yet. This needs to be included on vinta-schedule API and frontend

### 2.2. invite the user on vinta-schedule

Providers need to be invited to Vinta Schedule so they authorize it to look at their calendars in Google Calendar.

### 2.3. user accepts the invitation

Users will create their account in the Vinta Schedule and that will trigger the "user created" webhook.

### 2.4. user id is linked to the medplum provider

A Medplum bot will be triggered on user creation and that bot needs to add a vinta-schedule identifier to the Provider (so they are linked) and generate a Public API token to give the provider ability to make the necessary queries and mutations to Vinta Schedule.

### 2.5. user configures their calendars

They'll be able to configure what's their default calendar, what of their calendar should be listed and what calendars sync automatically, for instance. This way we ensure we're checking their availability correctly.

## 3\. How does the admin integrate medplum with vinta-schedule?

### 3.1. Create an admin Public API token

This token needs to allow resources management, calendar groups management, and calendar bundles management, and Public API tokens management.

### 3.2. Create webhooks for generating user tokens

On "user creation", generate a new provider token that only has access to manage that provider's data (manage recurring availability, manage specific availability dates, manage blocked times, free/busy checks, list events, list blocked times, schedule events)

#### Observation:

vinta-schedule doesn't have user-specific Public API tokens that are restricted to what the user has access to. This needs to be implemented to enable ([3.2](#32-on-user-creation-generate-a-new-provider-token-that-only-has-access-to-manage-that-providers-data-manage-recurring-availability-manage-specific-availability-dates-manage-blocked-times-freebusy-checks-list-events-list-blocked-times-schedule-events)).

### 3.3. Create Patient token

Create patient token that's only able to check availability and create appointment (passing the scheduling code or not depending if the calendars/calendar-groups/calendar-bundles is restricted)

#### Observation:

vinta-schedule doesn't differentiate restricted/public calendars/calendar-groups/calendar-bundles. This needs to be implemented before creating tokens for patients ([3.3](#33-generate-patient-token-thats-only-able-to-check-availability-and-create-appointment-passing-the-scheduling-code-or-not-depending-if-the-calendarscalendar-groupscalendar-bundles-is-restricted)). We also need:

* a mutation to generate a single-use scheduling code.  
* a mutation to generate a single-use rescheduling code (that only allow rescheduling one specific event).  
* a mutation to generate a single-use cancel code (that only allow cancelling one specific event).  
* a mutation to create an event passing a single-use scheduling code.  
* a mutation to reschedule an event passing a single-use scheduling code.

### 3.4. Implement single-use scheduling codes for Patients

Generate appointment type unique, single-use scheduling code so patients can schedule an appointment on restricted calendars/calendar-groups/calendar-bundles

## 4\. How do the events get synchronized between VintaSchedule and the Building Blocks?

When we create an appointment on the Building Blocks we also need to create a CalendarEvent on VintaSchedule so we save the CalendarEvent id in the Appointment (as an identifier)

We also need to create webhooks so updates in VintaSchedule are automatically cascaded into the Appointment. We'll need Medplum Bots to receive the webhooks and sync the Appointments.

# Integration touch-points per screen

Here we're going to describe which queries and mutations are going to be necessary for each page in the Provider/Admin App and in the Patient Portal. This will help us understand the gaps we have on the Public API.

## Provider/Admin App

### Login / SSO

* Maybe List calendars

### Location Page

* List resources (id, name, description, capacity)  
* createResourceCalendar(name, description, capacity)  
* disableResourceCalendar(id)  
* editResourceCalendar(name, description, capacity)  
  * This one only works with manual resource calendars, not with the ones synced with Google Calendar.

### Appointment Types & Calendar Groups & Bundles (Admin)

* List calendar groups (name, is\_private, slots { nodes { id, calendars { nodes { id, owners { nodes { id, user { id, email, profile { first\_name, last\_name, profile\_picture } } } } } })  
* List calendar bundles (name, is\_private, children { nodes { id, owners { nodes { id, user { id, email, profile { first\_name, last\_name, profile\_picture } } } } } })  
* createCalendarGroup(name, is\_private, slots)  
* updateCalendarGroup(name, is\_private, slots)  
* disableCalendarGroup(id)  
* List calendars, filter by user  
* createCalendarBundle(name, is\_private, childrenIds)  
* updateCalendarBundle(name, is\_private, childrenIds)  
* disableCalendarBundle(id)

### Provider Availability

* List available times  
* List unavailable times  
* List AvailabilityWindows  
* List BlockedTimes  
* createAvailabilityWindow  
* createBlockedTime  
* updateAvailabilityWindow  
* batchUpdateAvailabilityWindows  
* updateBlockedTime  
* deleteAvailabilityWindow  
* deleteBlockedTime

### Scheduler / Calendar

* List events (filter by user and calendar)  
  * We'll need to get matching appointments to display clinical information that won't be on Vinta Schedule

### Create Appointment Modal

* List resources  
* List calendar available times  
* List user available times  
* List calendar group available times  
* createCalendarEvent  
* createCalendarGroupEvent

### Booking Link Creation

* createCalendarBookingCode(calendar\_id)  
* createCalendarGroupBookingCode(calendar\_group\_id)  
* createCalendarRescheduleBookingCode(calendar\_id)  
* createCalendarGroupRescheduleBookingCode(calendar\_group\_id)  
* createCalendarCancellationBookingCode(calendar\_id)  
* createCalendarGroupCancellationBookingCode(calendar\_group\_id)

### Appointment Details

* Get CalendarEvent

### Reschedule / Cancel Modal

* List resources  
* List calendar available times  
* List user available times  
* List calendar group available times  
* rescheduleCalendarEvent()  
* rescheduleCalendarGroupEvent()  
* cancelEvent()

## Patient Portal

### Login Identification

Doesn't have integration

### Home / Dashboard

Doesn't have integration

### Booking Calendar

* List resources  
* List calendar available times  
* List user available times  
* List calendar group available times

### Booking Confirmation

* createCalendarEvent  
* createCalendarGroupEvent

### Intake Flag

Doesn't have integration

### Manage Appointment

* List resources  
* List calendar available times  
* List user available times  
* List calendar group available times  
* rescheduleCalendarEventWithCode()  
* rescheduleCalendarGroupEventWithCode()  
* cancelEventWithCode()

### Pre-visit Questionnaire

Doesn't have integration

### Visit Day

Doesn't have integration

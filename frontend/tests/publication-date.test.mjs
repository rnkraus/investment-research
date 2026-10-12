import assert from "node:assert/strict";
import test from "node:test";

import {
  formatPublicationDate,
  getLocalPublicationDate,
} from "../src/features/archive/publication-date.ts";

test("publication dates use zero-padded local calendar values", () => {
  assert.equal(getLocalPublicationDate(new Date(2026, 0, 2, 12)), "2026-01-02");
  assert.equal(
    getLocalPublicationDate(new Date(2028, 1, 29, 12)),
    "2028-02-29",
  );
});

test("publication dates follow the local day on either side of UTC midnight", (context) => {
  const previousTimezone = process.env.TZ;
  context.after(() => {
    if (previousTimezone === undefined) {
      delete process.env.TZ;
    } else {
      process.env.TZ = previousTimezone;
    }
  });

  process.env.TZ = "Pacific/Kiritimati";
  assert.equal(
    getLocalPublicationDate(new Date("2026-10-10T12:30:00Z")),
    "2026-10-11",
  );

  process.env.TZ = "America/Los_Angeles";
  assert.equal(
    getLocalPublicationDate(new Date("2026-10-11T01:30:00Z")),
    "2026-10-10",
  );
});

test("citation publication dates preserve the uploaded UTC day across timezones", (context) => {
  const previousTimezone = process.env.TZ;
  context.after(() => {
    if (previousTimezone === undefined) {
      delete process.env.TZ;
    } else {
      process.env.TZ = previousTimezone;
    }
  });

  for (const timezone of ["America/Los_Angeles", "Pacific/Kiritimati"]) {
    process.env.TZ = timezone;
    assert.equal(formatPublicationDate("2026-02-18T00:00:00Z"), "2026-02-18");
  }
});

test("unknown publication dates remain unknown rather than becoming today's date", () => {
  assert.equal(formatPublicationDate(null), undefined);
  assert.equal(formatPublicationDate(undefined), undefined);
});

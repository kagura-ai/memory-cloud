/**
 * adminUserPath (#1861): one place for every admin table that links a user.
 */

import { describe, expect, it } from "vitest";

import { adminUserPath } from "./admin";

describe("adminUserPath", () => {
  it("points at the admin user page", () => {
    expect(adminUserPath("104714482900000")).toBe(
      "/admin/users/104714482900000",
    );
  });

  it("encodes ids that carry a colon (local / connector identities)", () => {
    expect(adminUserPath("local:admin")).toBe("/admin/users/local%3Aadmin");
  });
});

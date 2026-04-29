import { test, expect } from "bun:test";
import orgLlmPlugin from "../src/index";

test("plugin entry point is a function", () => {
  expect(typeof orgLlmPlugin).toBe("function");
});

test("plugin entry point accepts an empty context", () => {
  const result = orgLlmPlugin({});
  expect(result).toBeUndefined();
});

test("plugin entry point accepts a context with version string", () => {
  const result = orgLlmPlugin({ opencodeVersion: "1.14.29" });
  expect(result).toBeUndefined();
});

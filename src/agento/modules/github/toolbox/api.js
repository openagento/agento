import express from 'express';
import { createOpenPrsHandler, createVerifyHandler } from './api-handlers.js';

export function register(server, { app, log, loadModuleConfigs, loadScopedDbOverrides }) {
  // The toolbox calls every module's register() TWICE: once at startup via registerModuleRestApis()
  // (context has `app` + `loadModuleConfigs`) and again on EVERY MCP session via registerTools()
  // (context has NO `loadModuleConfigs`). Without this guard the routes would be re-registered per
  // session (Express stacks duplicate handlers → leak). Register REST routes ONLY at startup.
  if (!app || !loadModuleConfigs) return;

  app.post('/api/github/verify', express.json(), createVerifyHandler(log));
  app.post(
    '/api/github/open-prs',
    express.json(),
    createOpenPrsHandler({ loadScopedDbOverrides, loadModuleConfigs }, log),
  );
}

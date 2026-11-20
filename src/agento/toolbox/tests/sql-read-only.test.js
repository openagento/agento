import { describe, expect, it } from 'vitest';
import { isReadOnlySql } from '../adapters/sql-read-only.js';

const MYSQL_STARTS = ['SELECT', 'SHOW', 'DESCRIBE', 'EXPLAIN', 'WITH'];
const MSSQL_STARTS = ['SELECT', 'WITH'];

describe('SQL read-only validation', () => {
  it.each([
    'SELECT * FROM report',
    'SELECT * FROM report;',
    "SELECT 'DELETE FROM report; -- text only' AS sample",
    '/* report */ WITH rows AS (SELECT 1 AS id) SELECT * FROM rows',
  ])('allows one read-only statement: %s', query => {
    expect(isReadOnlySql(query, MSSQL_STARTS, { dialect: 'mssql' })).toBe(true);
  });

  it.each([
    'WITH rows AS (SELECT 1 AS id) DELETE FROM report',
    'SELECT 1; DELETE FROM report',
    'SELECT * INTO report_copy FROM report',
    'SELECT 1; -- trailing comment\nDELETE FROM report',
    'SELECT * FROM #temp; DELETE FROM report',
    "SELECT '\\'; DELETE FROM report --'",
    "SELECT * FROM OPENQUERY([linked], 'DELETE FROM report RETURNING *')",
    'EXEC sp_help',
    '/* unterminated',
  ])('blocks a write or batch bypass: %s', query => {
    expect(isReadOnlySql(query, MSSQL_STARTS, { dialect: 'mssql' })).toBe(false);
  });

  it('keeps MySQL metadata statements while blocking mutating CTEs', () => {
    const options = { dialect: 'mysql' };
    expect(isReadOnlySql('SHOW TABLES', MYSQL_STARTS, options)).toBe(true);
    expect(isReadOnlySql('SHOW CREATE TABLE report', MYSQL_STARTS, options)).toBe(true);
    expect(isReadOnlySql('DESCRIBE report', MYSQL_STARTS, options)).toBe(true);
    expect(isReadOnlySql('WITH rows AS (SELECT 1) UPDATE report SET value = 1', MYSQL_STARTS, options)).toBe(false);
    expect(isReadOnlySql('SELECT 1 # comment', MYSQL_STARTS, options)).toBe(true);
    expect(isReadOnlySql('SELECT 1-- comment', MYSQL_STARTS, options)).toBe(true);
    expect(isReadOnlySql("SELECT 1--@x INTO OUTFILE '/tmp/x'", MYSQL_STARTS, options)).toBe(false);
    expect(isReadOnlySql('SELECT 1 /*!50000; DELETE FROM report */', MYSQL_STARTS, options)).toBe(false);
    expect(isReadOnlySql('SELECT 1 /* outer /* inner */; DELETE FROM report */', MYSQL_STARTS, options)).toBe(false);
  });

  it('fails closed for PostgreSQL escape-string ambiguity', () => {
    const query = "SELECT E'foo\\'bar'; DELETE FROM report --'";
    expect(isReadOnlySql(query, ['SELECT', 'WITH'], { dialect: 'postgresql' })).toBe(false);
  });

  it.each([
    ["SELECT * FROM t WHERE name LIKE 'a\\_b'", 'LIKE escape'],
    ["SELECT REGEXP_REPLACE(s, '(a)\\1', 'x') FROM t", 'regex back-reference'],
    ["SELECT 'a\\\\' AS s", 'even run before the delimiter'],
    ["SELECT 'a\\\\''b'", 'even run, then a doubled quote'],
    ['SELECT `we\\ird` FROM t', 'backslash in a backtick identifier'],
    ["SELECT REPLACE(name, 'a', 'b') FROM t", 'REPLACE() string function'],
    ["SELECT replace /* c */ (name, 'a', 'b') FROM t", 'REPLACE () with a comment before the parenthesis'],
  ])('allows MySQL read-only query: %s (%s)', query => {
    expect(isReadOnlySql(query, MYSQL_STARTS, { dialect: 'mysql' })).toBe(true);
  });

  it.each([
    ["SELECT 'a\\'; DELETE FROM t -- '", 'odd run before the delimiter'],
    ["SELECT 'a\\\\\\'; DELETE FROM t -- '", 'run of three before the delimiter'],
    ['SELECT "a\\"; DELETE FROM t -- "', 'odd run before a double quote'],
    ['SELECT `a\\`; DELETE FROM t -- `', 'odd run before a backtick'],
    ["SELECT 'unterminated\\", 'backslash at end of input'],
    ['REPLACE INTO t VALUES (1)', 'REPLACE INTO statement'],
    ['REPLACE t (a) VALUES (1)', 'REPLACE statement without INTO'],
    ['REPLACE(1)', 'statement that starts with REPLACE('],
    ['EXPLAIN REPLACE INTO t VALUES (1)', 'REPLACE behind EXPLAIN'],
    ['EXPLAIN REPLACE t SET a = 1', 'REPLACE SET behind EXPLAIN'],
    ["SELECT REPLACE(a, 'x', 'y') FROM t; REPLACE INTO t VALUES (1)", 'REPLACE() then a second statement'],
    ['EXPLAIN REPLACE `t` (a) VALUES (1)', 'REPLACE statement with a backtick table name'],
    ['DESCRIBE REPLACE `t` (a) VALUES (1)', 'REPLACE statement behind DESCRIBE'],
    ['EXPLAIN REPLACE "t" (a) VALUES (1)', 'REPLACE statement with a double-quoted table name'],
    ['EXPLAIN REPLACE /* c */ `t` (a) SELECT 1', 'REPLACE statement with a comment, then a quoted table name'],
    ['EXPLAIN REPLACE   (a) VALUES (1)', 'REPLACE statement with a non-ASCII identifier MySQL does not treat as space'],
  ])('blocks MySQL query: %s (%s)', query => {
    expect(isReadOnlySql(query, MYSQL_STARTS, { dialect: 'mysql' })).toBe(false);
  });

  it('allows an even backslash run in a PostgreSQL escape string', () => {
    expect(isReadOnlySql("SELECT E'a\\\\\\\\'", ['SELECT', 'WITH'], { dialect: 'postgresql' })).toBe(true);
  });

  it('allows the MSSQL REPLACE() string function', () => {
    expect(isReadOnlySql("SELECT REPLACE(name, 'a', 'b') FROM t", MSSQL_STARTS, { dialect: 'mssql' })).toBe(true);
  });
});

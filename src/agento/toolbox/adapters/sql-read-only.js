const WRITE_KEYWORDS = new Set([
  'ALTER', 'BACKUP', 'BEGIN', 'BULK', 'CALL', 'COMMIT', 'CREATE', 'DBCC',
  'DECLARE', 'DELETE', 'DENY', 'DROP', 'EXEC', 'EXECUTE', 'GRANT', 'INSERT',
  'INTO', 'KILL', 'LOAD', 'MERGE', 'OPENDATASOURCE', 'OPENQUERY', 'OPENROWSET',
  'REPLACE', 'RESTORE', 'REVOKE', 'ROLLBACK', 'SAVE', 'SET', 'SHUTDOWN',
  'TRANSACTION', 'TRUNCATE', 'UPDATE', 'UPSERT', 'USE',
]);

// MySQL token whitespace is ASCII only; JS \s also matches U+00A0 and other characters that MySQL
// reads as identifier text: https://dev.mysql.com/doc/refman/8.4/en/identifiers.html
const OPENING_PARENTHESIS_AFTER_SQL_WHITESPACE = /^[ \t\n\v\f\r]*\(/;

const DIALECTS = {
  mssql: {
    hashComments: false,
    nestedBlockComments: true,
    dashCommentRequiresWhitespace: false,
    backtickIdentifiers: false,
    bracketIdentifiers: true,
    rejectBackslashInQuotes: false,
  },
  mysql: {
    hashComments: true,
    nestedBlockComments: false,
    dashCommentRequiresWhitespace: true,
    backtickIdentifiers: true,
    bracketIdentifiers: false,
    rejectBackslashInQuotes: true,
  },
  postgresql: {
    hashComments: false,
    nestedBlockComments: true,
    dashCommentRequiresWhitespace: false,
    backtickIdentifiers: false,
    bracketIdentifiers: false,
    rejectBackslashInQuotes: true,
  },
};

function isWhitespaceOrControl(char) {
  return char === undefined || /[\s\u0000-\u001f\u007f]/u.test(char);
}

function sanitizedSql(query, dialectName) {
  if (typeof query !== 'string') return null;
  const dialect = DIALECTS[dialectName];
  if (!dialect) return null;

  let result = '';
  let state = 'normal';
  let blockDepth = 0;

  for (let i = 0; i < query.length; i += 1) {
    const char = query[i];
    const next = query[i + 1];

    if (state === 'line-comment') {
      if (char === '\n' || char === '\r') {
        state = 'normal';
        result += char;
      } else {
        result += ' ';
      }
      continue;
    }

    if (state === 'block-comment') {
      if (dialect.nestedBlockComments && char === '/' && next === '*') {
        blockDepth += 1;
        result += '  ';
        i += 1;
      } else if (char === '*' && next === '/') {
        blockDepth -= 1;
        result += '  ';
        i += 1;
        if (blockDepth === 0) state = 'normal';
      } else {
        result += ' ';
      }
      continue;
    }

    if (state !== 'normal') {
      const closing = state === 'single-quote' ? "'"
        : state === 'double-quote' ? '"'
          : state === 'backtick' ? '`' : ']';
      result += ' ';
      if (dialect.rejectBackslashInQuotes && char === '\\') {
        let backslashRunEnd = i;
        while (query[backslashRunEnd + 1] === '\\') backslashRunEnd += 1;
        const backslashRunIsOdd = (backslashRunEnd - i + 1) % 2 === 1;
        const literalEndDependsOnSqlMode = backslashRunIsOdd && query[backslashRunEnd + 1] === closing;
        if (literalEndDependsOnSqlMode) return null;
        result += ' '.repeat(backslashRunEnd - i);
        i = backslashRunEnd;
        continue;
      }
      if (char === closing) {
        if (next === closing) {
          result += ' ';
          i += 1;
        } else {
          state = 'normal';
        }
      }
      continue;
    }

    const dashComment = char === '-' && next === '-'
      && (!dialect.dashCommentRequiresWhitespace || isWhitespaceOrControl(query[i + 2]));
    if (dialectName === 'mysql' && char === '/' && next === '*' && (
      query[i + 2] === '!' || query.slice(i + 2, i + 4).toUpperCase() === 'M!'
    )) {
      return null;
    }
    if (dashComment || (char === '/' && next === '*')) {
      state = dashComment ? 'line-comment' : 'block-comment';
      blockDepth = state === 'block-comment' ? 1 : 0;
      result += '  ';
      i += 1;
    } else if (dialect.hashComments && char === '#') {
      state = 'line-comment';
      result += ' ';
    } else if (char === "'") {
      state = 'single-quote';
      result += char;
    } else if (char === '"') {
      state = 'double-quote';
      result += char;
    } else if (dialect.backtickIdentifiers && char === '`') {
      state = 'backtick';
      result += char;
    } else if (dialect.bracketIdentifiers && char === '[') {
      state = 'bracket';
      result += char;
    } else {
      result += char;
    }
  }

  return state === 'normal' || state === 'line-comment' ? result : null;
}

export function isReadOnlySql(query, allowedStartKeywords, { dialect = 'mssql' } = {}) {
  const sanitized = sanitizedSql(query, dialect);
  if (sanitized === null) return false;

  const trimmed = sanitized.trim();
  if (!trimmed) return false;
  const withoutTrailingSemicolon = trimmed.endsWith(';')
    ? trimmed.slice(0, -1).trimEnd()
    : trimmed;
  if (withoutTrailingSemicolon.includes(';')) return false;

  const isReplaceFunctionCallAfterStatementStart = (match, position) => position > 0
    && match[0].toUpperCase() === 'REPLACE'
    && OPENING_PARENTHESIS_AFTER_SQL_WHITESPACE.test(withoutTrailingSemicolon.slice(match.index + match[0].length));
  const tokens = [...withoutTrailingSemicolon.matchAll(/[A-Za-z_][A-Za-z0-9_$#]*/g)]
    .filter((match, position) => !isReplaceFunctionCallAfterStatementStart(match, position))
    .map(match => match[0].toUpperCase());
  if (tokens.length === 0 || !allowedStartKeywords.includes(tokens[0])) return false;
  const allowedMetadataKeywords = tokens[0] === 'SHOW' ? new Set(['CREATE']) : new Set();
  if (tokens.some(token => WRITE_KEYWORDS.has(token) && !allowedMetadataKeywords.has(token))) return false;
  if (tokens[0] === 'WITH' && !tokens.includes('SELECT')) return false;
  return true;
}

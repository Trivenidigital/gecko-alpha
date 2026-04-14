import React from 'react'

/**
 * Renders a token identifier as a clickable link to DexScreener or CoinGecko.
 *
 * Props:
 *   tokenId: string — contract address, CoinGecko coin slug, or category slug
 *   symbol: string (optional) — ticker symbol for display
 *   pipeline: string (optional) — "memecoin" or "narrative"
 *   chain: string (optional) — "solana", "ethereum", "base" etc
 *   type: string (optional) — "coin" (default), "category", or "auto"
 *   maxLen: number (optional) — truncate display to this length (default 16)
 */
export default function TokenLink({ tokenId, symbol, pipeline, chain, type = 'auto', maxLen = 16 }) {
  if (!tokenId) return <span>-</span>

  // Determine if this looks like a contract address (hex or base58)
  const isContractAddress = tokenId.startsWith('0x') || /^[1-9A-HJ-NP-Za-km-z]{32,}$/.test(tokenId)

  let href
  if (type === 'category') {
    // CoinGecko category page
    href = `https://www.coingecko.com/en/categories/${tokenId}`
  } else if (chain === 'coingecko' || (!isContractAddress && pipeline !== 'memecoin')) {
    // CoinGecko coin page — for chain="coingecko" or slug-like IDs
    href = `https://www.coingecko.com/en/coins/${tokenId}`
  } else if (isContractAddress || pipeline === 'memecoin') {
    // DexScreener link — only for actual contract addresses
    if (chain && chain !== 'coingecko') {
      href = `https://dexscreener.com/${chain}/${tokenId}`
    } else {
      href = `https://dexscreener.com/search?q=${tokenId}`
    }
  } else {
    // Fallback: CoinGecko coin page
    href = `https://www.coingecko.com/en/coins/${tokenId}`
  }

  const displayText = symbol || (tokenId.length > maxLen ? tokenId.slice(0, maxLen) + '...' : tokenId)

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      style={{
        color: '#4fc3f7',
        textDecoration: 'none',
        fontWeight: 600,
        fontFamily: symbol ? 'inherit' : 'monospace',
        fontSize: symbol ? 'inherit' : 11,
      }}
      title={tokenId}
    >
      {displayText} ↗
    </a>
  )
}

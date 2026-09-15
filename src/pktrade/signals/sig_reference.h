#pragma once

// SigReference: a reference-price Signal for HIP-4 relative-value market making.
//
// Unlike every other signal, this one does NOT read from the internal md beacon / pkmultifeed
// pipeline. It opens its OWN connections to external prediction markets (Kalshi over an
// authenticated WebSocket, Polymarket over REST), builds a weighted composite midpoint, and
// exposes it through the ordinary Signal interface (getValue / getBBMeasure / getBAMeasure /
// getValid). That lets a rel_wide strategy consume it as its `remote_sig` exactly like a
// SigQuoteMid, so the existing fair-value/basis machinery is reused unchanged.
//
// Ported from hip4maker: kalshi_stream.py (WS auth + snapshot/delta book), clients.py /
// adapters/{kalshi,polymarket}.py (REST parsing), references.py (weighted combine).
//
// Threading: the Kalshi WebSocket delivers messages on ixwebsocket's own thread. That thread
// only updates internal ladder state under a mutex and recomputes the composite midpoint into
// plain members guarded by the same mutex. It NEVER runs strategy code. A periodic publish
// timer running on the main event-loop thread snapshots the composite and calls setVV /
// broadcastValue, so all listener callbacks happen on the event-loop thread like any signal.

#include "signal.h"

#include <cpr/cpr.h>
#include <rapidjson/document.h>

#include <ixwebsocket/IXWebSocket.h>

#include <openssl/evp.h>

#include <map>
#include <mutex>
#include <string>
#include <vector>

namespace pktrade::signals {

// One venue's top-of-book in canonical YES terms (probabilities in [0, 1]).
struct RefVenueBook {
  double bid = 0.0;
  double ask = 1.0;
  double bid_size = 0.0;
  double ask_size = 0.0;
  int64_t source_ts_ms = 0;  // venue-stamped time, if given
  int64_t recv_ts_ms = 0;    // when we received it
  double weight = 1.0;
  bool valid = false;

  double mid() const { return 0.5 * (bid + ask); }
};

class SigReference : public Signal {
 public:
  SigReference(int signal_id, const rapidjson::Value& sig_conf, SignalFactory* sf);
  ~SigReference() override;

  bool isSame(const rapidjson::Value& other_conf, SignalFactory& sf,
              const rapidjson::Value& all_signals_array) const override;

  // No internal books: this signal sources its data from external venues, not the beacon.
  std::vector<pktrade::BookId> getBookIds() const override { return {}; }

  // Called during signal setup. We don't use the beacon; instead we schedule our external
  // connections to start once the event loop / trading start time is reached.
  void subscribeData(MDBeacon* beacon) override;

  void reset() override;

  // Book-like measures for the composite reference. Read under the lock.
  double getBBMeasure() override;
  double getBAMeasure() override;
  double getBBSize() override;
  double getBASize() override;

  std::string getPrimarySymbol() const override { return kalshi_ticker_; }

 protected:
  // ---- lifecycle ----
  void startConnections();     // opens Kalshi WS, kicks off Polymarket + tie REST polls
  void schedulePublish();      // arms the event-loop publish timer
  void publishTick();          // event-loop thread: snapshot composite -> setVV + broadcast

  // ---- Kalshi WebSocket ----
  void connectKalshiWs();
  std::vector<std::string> kalshiAuthHeaders() const;    // RSA-PSS signed access headers
  void onKalshiMessage(const std::string& raw);          // ws thread
  void applyKalshiSnapshot(const rapidjson::Value& msg, int64_t seq);
  void applyKalshiDelta(const rapidjson::Value& msg, int64_t seq);
  void invalidateKalshi(const std::string& reason);      // clears book, forces resnapshot
  void recomputeKalshiFromLevels(int64_t src_ts_ms);     // rebuild kalshi_book_ from ladders

  // ---- REST pollers (event-loop thread, cpr async) ----
  void pollKalshiRest();   // fallback when the WS book isn't ready
  void pollPolymarket();
  void pollTie();

  // ---- compose ----
  void recomputeComposite();  // mutex held by caller

  // ---- helpers ----
  int64_t nowMs() const;
  static std::string base64Encode(const unsigned char* data, size_t len);

  // ------------------------------------------------------------------
  // Config
  // ------------------------------------------------------------------
  bool testnet_ = false;

  // Kalshi
  std::string kalshi_ticker_;
  std::string kalshi_ws_url_;
  std::string kalshi_rest_url_;
  std::string kalshi_creds_file_;
  std::string kalshi_api_key_id_;
  EVP_PKEY* kalshi_pkey_ = nullptr;  // RSA private key for PSS signing (owned)
  double kalshi_weight_ = 1.0;
  bool kalshi_enabled_ = false;
  int kalshi_rest_poll_s_ = 2;  // REST fallback cadence when WS not ready

  // Polymarket (REST only; hip4maker has no Polymarket WS)
  std::string poly_token_id_;
  std::string poly_clob_url_;
  double poly_weight_ = 0.0;
  bool poly_enabled_ = false;
  int poly_poll_s_ = 2;

  // Optional Kalshi "tie" adjustment: shift the reference up by
  // tie_fraction_ * tie_mid_ (a draw/settlement bucket). REST-polled.
  std::string tie_ticker_;
  double tie_fraction_ = 0.5;
  bool tie_enabled_ = false;

  int publish_interval_ms_ = 100;
  int64_t reference_stale_after_ms_ = 5000;

  // ------------------------------------------------------------------
  // Runtime state (guarded by mu_)
  // ------------------------------------------------------------------
  mutable std::mutex mu_;

  ix::WebSocket kalshi_ws_;

  // Kalshi price ladders, keyed by price (probability). YES-bid and NO-bid sides, matching
  // hip4maker: best_bid = max(yes_levels), best_ask = min(no_levels).
  std::map<double, double> kalshi_yes_levels_;
  std::map<double, double> kalshi_no_levels_;
  int64_t kalshi_last_seq_ = -1;  // -1 == no snapshot yet
  bool kalshi_ready_ = false;

  RefVenueBook kalshi_book_;
  RefVenueBook poly_book_;
  double tie_mid_ = 0.0;
  bool tie_have_ = false;

  // Composite (what getValue publishes).
  double comp_bid_ = 0.0;
  double comp_ask_ = 1.0;
  double comp_bid_size_ = 0.0;
  double comp_ask_size_ = 0.0;
  int64_t comp_last_update_ms_ = 0;
  bool comp_valid_ = false;

  bool started_ = false;
};

} // namespace pktrade::signals

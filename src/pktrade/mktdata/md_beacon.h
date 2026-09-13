#pragma once

#include "pktrade/enums/basic_enums.h"
#include "pktrade/types.h"

#include "pktrade/mdapi.h"

#include "pktrade/book_manager.h"
#include "pktrade/mdapi.h"
#include <unordered_map>
#include <vector>

/*

  Contains both a collection of books and a collection of booklisteners.

  Runs through time, sequentially. Hears back callbacks from sub-books, and
  creates normalized callbacks for each of them.


 */

namespace pktrade::md {

// Eventually, add things like:
// OrdAdd, etc.
// Status updates
enum class BeaconCBType {
  LvlAdd,
  LvlMod,
  LvlDel,
  Trd,
  Quote,
  Final,
};

// Gets callbacks from MDBeacon.
class BeaconListener {
 public:
  virtual ~BeaconListener() = default;

  virtual void onLvlAdd(const LevelBook& bk, const LevelAdd& lvl_add) = 0;
  virtual void onLvlMod(const LevelBook& bk, const LevelModify& lvl_mod) = 0;
  virtual void onLvlDel(const LevelBook& bk, const LevelDelete& lvl_del) = 0;
  virtual void onTrade(const LevelBook& bk, const Trade& trd) = 0;

  // Most things don't need to implement this.
  virtual void onQuote(const LevelBook& bk, const Quote& quote) {};

  // Override if you care about final calls.
  virtual void onFinal(const LevelBook& bk) = 0;

  const int getPri() const { return pri_; }

 protected:
  // Low priority comes first.
  int pri_ = 75;
};

class MDBeacon : public BookListener {
 public:
  // Callbacks from the Book

  MDBeacon(BookManager* bm, std::vector<BookId>::iterator bid_begin,
           std::vector<BookId>::iterator bid_end, bool always_deliver = false);
  ~MDBeacon(){};

  void addListener(const BookId& book_id, BeaconListener* listener,
                   std::vector<BeaconCBType> cb_types);

  void subscribeAndPrepare(BookManager* bm);

  // Callbacks from the direct books.
  void onLevelUpdate(const LevelBook& bk, const LevelAdd& add) override;
  void onLevelUpdate(const LevelBook& bk, const LevelModify& mod) override;
  void onLevelUpdate(const LevelBook& bk, const LevelDelete& del) override;
  void onLevelUpdate(const LevelBook& bk, const Trade& trd) override;
  void onQuote(const LevelBook& bk, const Quote& quote) override;

  void onFinal(const LevelBook& bk) override;

  bool outOfBounds();

 protected:
  std::unordered_map<BookId, std::vector<BeaconListener*>> lvl_add_listeners_;
  std::unordered_map<BookId, std::vector<BeaconListener*>> lvl_mod_listeners_;
  std::unordered_map<BookId, std::vector<BeaconListener*>> lvl_del_listeners_;
  std::unordered_map<BookId, std::vector<BeaconListener*>> trd_listeners_;
  std::unordered_map<BookId, std::vector<BeaconListener*>> quote_listeners_;

  std::unordered_map<BookId, std::vector<BeaconListener*>> final_listeners_;

  std::vector<BookId> book_ids_;
  BookManager* bm_;
  // std::unordered_map<Market, std::unordered_map<std::string, int>> bid_map_;

  // For outofbounds: give this much leeway around the daystart and end.
  std::chrono::seconds bound_tolerance_ = std::chrono::seconds(60);

  // If true, then we always
  bool always_deliver_ = false;
};

} // namespace pktrade::md

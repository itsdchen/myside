#include "md_beacon.h"

#include "pktrade/context/global_vars.h"
#include "pktrade/util/basiclib.h"
#include "pktrade/util/time_utils.h"
#include <algorithm>
#include <magic_enum.hpp>

namespace pktrade::md {

MDBeacon::MDBeacon(BookManager* bm, std::vector<BookId>::iterator bid_begin,
                   std::vector<BookId>::iterator bid_end, bool always_deliver)
    : bm_(bm), book_ids_(bid_begin, bid_end), always_deliver_(always_deliver) {
  // For each book_id, add a... entry in the listeners.

  for (auto x = bid_begin; x != bid_end; ++x) {
    //    std::cout << "Adding book_id " << magic_enum::enum_name((*x).first) <<
    //    " " << (*x)uu.second << " to beacon.\n";

    lvl_add_listeners_.insert({*x, {}});
    lvl_mod_listeners_.insert({*x, {}});
    lvl_del_listeners_.insert({*x, {}});
    trd_listeners_.insert({*x, {}});
    quote_listeners_.insert({*x, {}});
    final_listeners_.insert({*x, {}});
  }
}

void MDBeacon::addListener(const BookId& book_id, BeaconListener* listener,
                           std::vector<BeaconCBType> cb_types) {
  // Dedup each one.
  for (BeaconCBType cb_type : cb_types) {
    switch (cb_type) {
      case BeaconCBType::LvlAdd:
        if (std::find(lvl_add_listeners_[book_id].begin(), lvl_add_listeners_[book_id].end(),
                      listener) == lvl_add_listeners_[book_id].end()) {
          lvl_add_listeners_[book_id].push_back(listener);
        }
        break;
      case BeaconCBType::LvlMod:
        if (std::find(lvl_mod_listeners_[book_id].begin(), lvl_mod_listeners_[book_id].end(),
                      listener) == lvl_mod_listeners_[book_id].end()) {
          lvl_mod_listeners_[book_id].push_back(listener);
        }
        break;
      case BeaconCBType::LvlDel:
        if (std::find(lvl_del_listeners_[book_id].begin(), lvl_del_listeners_[book_id].end(),
                      listener) == lvl_del_listeners_[book_id].end()) {
          lvl_del_listeners_[book_id].push_back(listener);
        }
        break;
      case BeaconCBType::Trd:
        if (std::find(trd_listeners_[book_id].begin(), trd_listeners_[book_id].end(), listener) ==
            trd_listeners_[book_id].end()) {
          trd_listeners_[book_id].push_back(listener);
        }
        break;
      case BeaconCBType::Quote:
        if (std::find(quote_listeners_[book_id].begin(), quote_listeners_[book_id].end(),
                      listener) == quote_listeners_[book_id].end()) {
          quote_listeners_[book_id].push_back(listener);
        }
        break;
      case BeaconCBType::Final:
        if (std::find(final_listeners_[book_id].begin(), final_listeners_[book_id].end(),
                      listener) == final_listeners_[book_id].end()) {
          final_listeners_[book_id].push_back(listener);
        }
        break;

      default:
        throw std::runtime_error("Called MDBeacon::addListener with invalid cb_type.");
        break;
    }
  }
  // Always add them to final callbacks.
}

// This should happen after all the beacon's own listeners are added.
void MDBeacon::subscribeAndPrepare(BookManager* bm) {
  bm_ = bm;

  // Go through book_id_s, subscribe.
  for (BookId& book_id : book_ids_) {
    bm_->subscribe(book_id, this);

    // Go through all the listeners, and sort them by priority.
    std::stable_sort(
        lvl_add_listeners_[book_id].begin(), lvl_add_listeners_[book_id].end(),
        [](BeaconListener* a, BeaconListener* b) { return a->getPri() < b->getPri(); });
    std::stable_sort(
        lvl_mod_listeners_[book_id].begin(), lvl_mod_listeners_[book_id].end(),
        [](BeaconListener* a, BeaconListener* b) { return a->getPri() < b->getPri(); });
    std::stable_sort(
        lvl_del_listeners_[book_id].begin(), lvl_del_listeners_[book_id].end(),
        [](BeaconListener* a, BeaconListener* b) { return a->getPri() < b->getPri(); });
    std::stable_sort(
        trd_listeners_[book_id].begin(), trd_listeners_[book_id].end(),
        [](BeaconListener* a, BeaconListener* b) { return a->getPri() < b->getPri(); });
    std::stable_sort(
        quote_listeners_[book_id].begin(), quote_listeners_[book_id].end(),
        [](BeaconListener* a, BeaconListener* b) { return a->getPri() < b->getPri(); });

    std::stable_sort(
        final_listeners_[book_id].begin(), final_listeners_[book_id].end(),
        [](BeaconListener* a, BeaconListener* b) { return a->getPri() < b->getPri(); });
  }
}

void MDBeacon::onLevelUpdate(const LevelBook& bk, const LevelAdd& add) {
  // Compare against start/end times.
  if (outOfBounds()) {
    return;
  }
  // Flow through to its children.
  for (BeaconListener* listener : lvl_add_listeners_[bk.getBookID()]) {
    listener->onLvlAdd(bk, add);
  }
}

void MDBeacon::onLevelUpdate(const LevelBook& bk, const LevelModify& mod) {
  if (outOfBounds()) {
    return;
  }
  for (BeaconListener* listener : lvl_mod_listeners_[bk.getBookID()]) {
    listener->onLvlMod(bk, mod);
  }
}

void MDBeacon::onLevelUpdate(const LevelBook& bk, const LevelDelete& del) {
  if (outOfBounds()) {
    return;
  }
  for (BeaconListener* listener : lvl_del_listeners_[bk.getBookID()]) {
    listener->onLvlDel(bk, del);
  }
}

void MDBeacon::onLevelUpdate(const LevelBook& bk, const Trade& trd) {
  if (outOfBounds()) {
    return;
  }

  for (BeaconListener* listener : trd_listeners_[bk.getBookID()]) {
    listener->onTrade(bk, trd);
  }
}

void MDBeacon::onQuote(const LevelBook& bk, const Quote& quote) {
  if (outOfBounds()) {
    return;
  }

  for (BeaconListener* listener : quote_listeners_[bk.getBookID()]) {
    listener->onQuote(bk, quote);
  }
}

void MDBeacon::onFinal(const LevelBook& bk) {
  if (outOfBounds()) {
    return;
  }

  for (BeaconListener* listener : final_listeners_[bk.getBookID()]) {
    listener->onFinal(bk);
  }
}

// Right now, it's the beacon that imposes the start/end times.
// Later on, we should probably have that be handled by the programs themselves.
bool MDBeacon::outOfBounds() {

  // OK, we can run with a flag that says no such thing as out of bounds.
  // Then, we can deliver stuff whenever.

  if (always_deliver_) {
    return false;
  }

  if (Clock::now() < pktrade::GlobalVar::start_t_ - bound_tolerance_) {
    return true;
  }
  if (Clock::now() > pktrade::GlobalVar::end_t_ + bound_tolerance_) {
    return true;
  }
  return false;
}

} // namespace pktrade::md

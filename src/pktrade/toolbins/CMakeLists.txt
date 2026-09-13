
add_pk_executable(msgprint msgprint.cc)
target_link_libraries(msgprint PRIVATE pkcore util RapidJSON::RapidJSON)

add_pk_executable(livemsgprint livemsgprint.cc)
target_link_libraries(livemsgprint PRIVATE pkcore util RapidJSON::RapidJSON ncurses)


add_pk_executable(hello hello.cc)
target_link_libraries(hello PRIVATE pkcore util RapidJSON::RapidJSON)


add_pk_executable(metronome metronome.cc)
target_link_libraries(
  metronome
  PRIVATE fmt::fmt
          util
          mktdata
          tempos
          pkcore
          RapidJSON::RapidJSON
)

add_pk_executable(signalscanner signalscanner.cc)
target_link_libraries(
  signalscanner
  PRIVATE fmt::fmt
          util
          mktdata
          signals
          tempos
          pkcore
          RapidJSON::RapidJSON
)

add_pk_executable(simple_datalog simple_datalog.cc)
target_link_libraries(
  simple_datalog
  PRIVATE fmt::fmt
          OpenSSL::Crypto
          ixwebsocket::ixwebsocket
          nlohmann_json::nlohmann_json
          glog::glog
          cppzmq::cppzmq
          ZLIB::ZLIB
          pkproto
          pkcore
          util
)


add_pk_executable(supabase_test supabase_test.cc)
target_link_libraries(supabase_test PRIVATE fmt::fmt cpr::cpr util pkcore RapidJSON::RapidJSON)

add_pk_executable(supabase_realtime supabase_realtime.cc)
target_link_libraries(supabase_realtime PRIVATE fmt::fmt cpr::cpr util RapidJSON::RapidJSON ixwebsocket::ixwebsocket)


add_pk_executable(pkdex pkdex.cc)
target_link_libraries(pkdex PRIVATE fmt::fmt util pkcore RapidJSON::RapidJSON)

add_pk_executable(symstats symstats.cc)
target_link_libraries(
  symstats
  PRIVATE fmt::fmt
          util
          mktdata
          signals
          tempos
          pkcore
          RapidJSON::RapidJSON
)


add_pk_executable(returner returner.cc)
target_link_libraries(
  returner
  PRIVATE fmt::fmt
          util
          mktdata
          signals
          tempos
          pkcore
          RapidJSON::RapidJSON
)

add_pk_executable(markout markout.cc)
target_link_libraries(markout PRIVATE fmt::fmt util pkcore RapidJSON::RapidJSON)

add_pk_executable(fillstats fillstats.cc)
target_link_libraries(fillstats PRIVATE fmt::fmt util pkcore RapidJSON::RapidJSON)

add_pk_executable(vizdata vizdata.cc)
target_link_libraries(
  vizdata
  PRIVATE fmt::fmt
          util
          mktdata
          pkcore
          RapidJSON::RapidJSON
)

add_pk_executable(feedquality feedquality.cc)
target_link_libraries(
  feedquality
  PRIVATE fmt::fmt
          util
          mktdata
          pkcore
          RapidJSON::RapidJSON
)

add_pk_executable(hl_book_check hl_book_check.cc)
target_link_libraries(
  hl_book_check
  PRIVATE fmt::fmt
          magic_enum::magic_enum
          util
          pkcore
          cppzmq::cppzmq
)

add_pk_executable(hl_node_publisher hl_node_publisher.cc)
target_link_libraries(
  hl_node_publisher
  PRIVATE fmt::fmt
          util
          pkcore
          cppzmq::cppzmq
          RapidJSON::RapidJSON
          cpr::cpr
          ixwebsocket::ixwebsocket
)

# All toolbins pull librisk.a transitively via util -> context -> risk, but
# librisk.a has unresolved refs into libsim.a (SimVerse) and libpkt.a
# (TradeCarrier::getGlobalPosition). Link pkt — which PUBLIC-links sim — so
# those resolve. Drop this once risk stops calling into sim_verse_/trade_carrier_.
foreach(_toolbin
    msgprint livemsgprint hello metronome signalscanner simple_datalog
    supabase_test supabase_realtime pkdex symstats returner markout fillstats
    vizdata feedquality hl_book_check hl_node_publisher)
  target_link_libraries(${_toolbin} PRIVATE pkt)
endforeach()

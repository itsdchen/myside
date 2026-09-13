# https://github.com/protocolbuffers/protobuf/blob/main/docs/cmake_protobuf_generate.md#how-to-use-protobuf_generate

add_library(pkproto OBJECT mdmsg.proto gateway.proto)

target_link_libraries(pkproto PUBLIC protobuf::libprotobuf)
target_include_directories(
  pkproto PUBLIC $<BUILD_INTERFACE:${PROJECT_BINARY_DIR}/src/pktrade>
)
protobuf_generate(TARGET pkproto LANGUAGE cpp)
protobuf_generate(TARGET pkproto LANGUAGE python)

# I guess this adds the address sanitizer command. OK. For now, let's leave
# these as dev-based things (versus prod), because they might slow things down.
# add_compile_options(-fsanitize=address) add_link_options(-fsanitize=address)

# add_compile_options(-fsanitize=thread) add_link_options(-fsanitize=thread)

# add_compile_options(-fsanitize=leak) add_link_options(-fsanitize=leak)

add_pk_library(
  pkcore
  fixed_point.cc
  event_loop.cc
  pollable.cc
  timer.cc
  feed.cc
  book_manager.cc
  decoder.cc
)
target_link_libraries(
  pkcore
  PUBLIC Boost::boost
         namedtype::namedtype
         glog::glog
         fmt::fmt
         CLI11::CLI11
         magic_enum::magic_enum
         RapidJSON::RapidJSON
         cppzmq::cppzmq
         vincentlaucsb-csv-parser::vincentlaucsb-csv-parser
         date::date
         pkproto
         fast-cpp-csv-parser::fast-cpp-csv-parser
         ixwebsocket::ixwebsocket
)

add_subdirectory(context)
include_directories(enums)
add_subdirectory(feed)
add_subdirectory(feedrelay)
add_subdirectory(mktdata)
add_subdirectory(ordex)
add_subdirectory(pktrader)
add_subdirectory(probe)
add_subdirectory(protoio)
add_subdirectory(risk)
add_subdirectory(signals)
add_subdirectory(sim)
add_subdirectory(tempos)
add_subdirectory(toolbins)
add_subdirectory(util)

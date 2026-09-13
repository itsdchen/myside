# This takes in our xeisting json files, interprets them, and then reads them to
# proto.
add_pk_executable(jsontoproto jsontoproto.cc)

target_link_libraries(
  jsontoproto
  PRIVATE fmt::fmt
          util
          mktdata
          pkcore
          RapidJSON::RapidJSON
)

# This takes in our xeisting json files, interprets them, and then reads them to
# proto.
add_pk_executable(readpbf readpbf.cc)

target_link_libraries(
  readpbf
  PRIVATE fmt::fmt
          util
          mktdata
          pkcore
          pkproto
          ZLIB::ZLIB
          nlohmann_json::nlohmann_json
          RapidJSON::RapidJSON
)

# Postproc for simple_datalog --hlnode .pb.gz captures into per-(sym, stream)
# .gzpbf files matching the layout CombinedStreamSplitter + jsontoproto write.
add_pk_executable(hlnode_gzsplitter hlnode_gzsplitter.cc)
target_link_libraries(
  hlnode_gzsplitter
  PRIVATE fmt::fmt
          util
          pkcore
          pkproto
          ZLIB::ZLIB
)

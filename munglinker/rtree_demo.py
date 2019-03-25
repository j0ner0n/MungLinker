from rtree import index

rtree_index = index.Index()
rtree_index.add(0, (0, 0, 10, 10), obj="first_object")
rtree_index.add(1, (5, 5, 15, 15), obj="second_object")
rtree_index.add(2, (25, 25, 35, 35), obj="third_object")

for nearest_object in rtree_index.nearest((20, 15), objects=True):
    print(nearest_object.object)

for intersecting_objects in rtree_index.intersection((2, 2, 12, 12), objects=True):
    print(intersecting_objects.object)
